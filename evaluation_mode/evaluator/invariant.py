"""Invariant evaluator: structured evaluation of the REASONING PROCESS between the
source and the sink.

Division of labour:

  * source / sink are the two artifact-grounded ANCHORS (sink = crash stack,
    source = input load point). Their POSITION is scored separately by the
    endpoints evaluator (t1_source_sink) — a localization question.
  * this evaluator scores what happens BETWEEN the anchors: the intermediate KEY
    invariant checkpoints (root_cause, tainted_value_materialization, alloc, free,
    dispatch ...) and the typed edges (data / control / order) that connect the
    whole chain source -> ... -> sink.

Each GT marks KEY invariant checkpoints (fine_trace steps with `key: true`). A
between-node is scored JOINTLY on:

  1. POSITION: did the agent's recorded claim locate this point (file + line/function)?
  2. EDGES: did the agent record the typed depends_on edges INTO this point?

A between-node is `reasoned` only if it was BOTH located AND its edges established,
so reasoning is a connected chain, not isolated points. The edge INTO the sink (the
"why does it crash" control/order edge) is not a between-node but is the single most
important reasoning edge — it is captured in `edge_recall_by_type`, which covers every
typed edge of every key step. Deterministic; the agent's structured recorder state is
the input, not fuzzy trajectory text.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from recorder_core.core import normalize_record, reduce_records, role_group

from .base import BaseEvaluator, EvaluationInput
from .recorder_evidence import load_recorder_events
from .trajectory import path_suffix_matches

LINE_TOLERANCE = 3

# The two anchors — scored by the endpoints evaluator, excluded from reasoning nodes.
SOURCE_ROLES = {"source"}
SINK_ROLES = {"sink", "sink_access", "unsafe_use"}
ENDPOINT_ROLES = SOURCE_ROLES | SINK_ROLES


class InvariantEvaluator(BaseEvaluator):
    name = "invariant"
    version = "0.3"

    def evaluate(self, inputs: EvaluationInput) -> dict[str, Any]:
        gt = json.loads(inputs.ground_truth.read_text(encoding="utf-8"))
        agent, has_records = build_agent_state(inputs.ground_truth)

        # The scored invariants come from stage 04's verified_invariants.json (nodes/edges
        # it selected from artifacts AND verified by instrumentation). Fall back to the GT's
        # own `key` marks only when stage 04 has not run yet (transition).
        vi = _load_verified(inputs.ground_truth)
        if vi is not None:
            key_steps = _key_steps_from_verified(vi)
            gt_nodes = [(str(n.get("file") or ""), str(n.get("function") or ""),
                         _safe_int(n.get("line")), str(n.get("role") or "")) for n in key_steps]
            gt_edges = _gt_edge_index(key_steps, {s["step"]: str(s.get("var") or "") for s in key_steps})
            invariant_source = "verified_invariants"
        else:
            fine = gt.get("fine_trace") or []
            key_steps = [s for s in fine if isinstance(s, dict) and s.get("key")]
            gt_nodes = [(str(s.get("file") or ""), str(s.get("function") or ""),
                         _safe_int(s.get("line")), str(s.get("role") or "")) for s in fine if isinstance(s, dict)]
            gt_edges = _gt_edge_index(fine, {s.get("step"): str(s.get("var") or "") for s in fine if isinstance(s, dict)})
            invariant_source = "fine_trace_key"
        var_by_step = {s.get("step"): str(s.get("var") or "") for s in key_steps}

        # Between-nodes: key steps that are NOT the source/sink anchors. Scored
        # JOINTLY on position AND their incoming edges. Siblings (other key positions)
        # keep position matching line-discriminative when points share a function.
        siblings = [(str(s.get("function") or ""), _safe_int(s.get("line"))) for s in key_steps]
        between_steps = [s for s in key_steps if str(s.get("role") or "") not in ENDPOINT_ROLES]
        points = [self._eval_point(s, var_by_step, agent, siblings) for s in between_steps]

        # The sink anchor's POSITION is scored by the endpoints evaluator, but its
        # INCOMING edges (the "why-crash" control/order edge) are core reasoning, so
        # they form an edges-only reasoning unit here.
        sink_steps = [s for s in key_steps
                      if str(s.get("role") or "") in SINK_ROLES and s.get("depends_on")]
        sink_points = [self._eval_sink_edges(s, var_by_step, agent) for s in sink_steps]
        reasoning_units = points + sink_points

        # Edge recall covers EVERY typed edge of EVERY key step.
        edge_by_type: dict[str, dict[str, int]] = {}
        for s in key_steps:
            for e in _eval_edges(s, var_by_step, agent):
                slot = edge_by_type.setdefault(e["type"], {"total": 0, "matched": 0})
                slot["total"] += 1
                slot["matched"] += 1 if e["status"] == "matched" else 0

        total = len(reasoning_units)
        located = sum(1 for p in points if p["position"] == "located")
        reasoned = sum(1 for p in reasoning_units if p["reasoned"])
        edge_matched = sum(v["matched"] for v in edge_by_type.values())
        edge_total = sum(v["total"] for v in edge_by_type.values())

        # Precision: of everything the agent recorded, how much the verified invariant set
        # supports — guards against a "spray everything" agent scoring on recall alone.
        node_precision = _precision(agent["nodes"], lambda n: _node_supported(n, gt_nodes))
        edge_precision = _precision(agent["edges"], lambda e: _edge_supported(e, gt_edges))

        position_recall = _ratio(located, len(points))
        reasoning_recall = _ratio(reasoned, total)
        edge_recall = _ratio(edge_matched, edge_total)

        return {
            "evaluator": self.name,
            "version": self.version,
            "structured_reasoning_evaluable": has_records,
            "invariant_source": invariant_source,
            "reasoning_points": reasoning_units,
            "summary": {
                "reasoning_points": total,  # between-nodes + sink edge unit
                "located_points": located,
                "reasoned_points": reasoned,  # located AND edges established
                "position_recall": position_recall,  # located between-nodes (floor)
                "reasoning_recall": reasoning_recall,  # located AND connected (primary)
                "node_precision": node_precision,  # agent nodes the GT trace supports
                "node_f1": _f1(position_recall, node_precision),
                "edge_recall": edge_recall,
                "edge_precision": edge_precision,  # agent edges the GT trace supports
                "edge_f1": _f1(edge_recall, edge_precision),
                "edge_recall_by_type": {t: _ratio(v["matched"], v["total"]) for t, v in edge_by_type.items()},
            },
            "notes": [
                "Scores the reasoning BETWEEN source and sink; the anchors' positions are "
                "scored by the endpoints evaluator (t1_source_sink).",
                "reasoning_recall (between-node located AND its edges established) is the primary "
                "metric; position_recall alone would be single-point and is reported only as a floor.",
                "*_precision are over the agent's OWN claims vs the full GT trace, so recall and "
                "precision together penalize both missing and invented reasoning.",
                "No reasoning_points means the GT marked no between-node invariants — regenerate "
                "with `key: true` on root_cause / materialization / alloc / free steps.",
            ],
        }

    def _eval_point(self, step: dict, var_by_step: dict, agent: dict, siblings: list) -> dict[str, Any]:
        role = str(step.get("role") or "")
        candidates = _role_candidates(role, agent["nodes"])
        # root_cause carries a CRITERION {variable, condition, region} instead of one line —
        # any valid framing (same var + condition in the region) is credited, not a single line.
        criterion = step.get("criterion")
        if isinstance(criterion, dict) and (criterion.get("kind") or criterion.get("variable") or criterion.get("object")):
            position = _match_criterion(criterion, candidates)
        else:
            position = _match_position(step, candidates, siblings)
        edges = _eval_edges(step, var_by_step, agent)
        # A lifetime root_cause is only "reasoned" if the agent also expressed the temporal
        # relation (free_before_use / double_free) on the object — being at the free/use site
        # is not enough. This is what tightens the (looser) site-level position match.
        if isinstance(criterion, dict) and criterion.get("kind") == "lifetime":
            edges = edges + [_eval_lifetime_order(criterion, agent["edges"])]

        pos_ok = position["status"] == "located"
        edges_ok = all(e["status"] == "matched" for e in edges) if edges else True
        return {
            "step": step.get("step"),
            "role": role,
            "gt_location": {"file": step.get("file"), "function": step.get("function"), "line": step.get("line")},
            "position": position["status"],
            "position_agent": position["agent"],
            "edges": edges,
            "reasoned": pos_ok and edges_ok,
        }

    def _eval_sink_edges(self, step: dict, var_by_step: dict, agent: dict) -> dict[str, Any]:
        """The sink anchor as an edges-only reasoning unit: its incoming edges (the
        why-crash edge) must match; its position is scored by the endpoints evaluator."""
        edges = _eval_edges(step, var_by_step, agent)
        return {
            "step": step.get("step"),
            "role": str(step.get("role") or ""),
            "gt_location": {"file": step.get("file"), "function": step.get("function"), "line": step.get("line")},
            "position": "delegated_to_endpoints",
            "position_agent": None,
            "edges": edges,
            "reasoned": all(e["status"] == "matched" for e in edges) if edges else True,
        }


def build_agent_state(gt_path) -> tuple[dict[str, Any], bool]:
    """Reduce the recorder events bundled with the GT into the agent's reasoning
    trace: `nodes` (each carrying its finer `role`) and typed `edges`. Shared by all
    reasoning evaluators; GT key nodes are matched against `nodes` by role/group."""
    records = [normalize_record(e) for e in load_recorder_events(gt_path)]
    state = reduce_records(records) if records else {}
    agent = {
        "nodes": [n for n in (state.get("all_nodes") or []) if isinstance(n, dict)],
        "edges": [e for e in (state.get("trace") or []) if isinstance(e, dict)],
    }
    # Optionally fold in the GT-blind observer's reconstructed reasoning (recovers what
    # the agent reasoned but did not record). Off by default → no behaviour change.
    if os.getenv("GT_EVAL_USE_OBSERVER"):
        try:
            from external_interpreter.observer import load_observer_trace, merge_observer_into_agent
            from .recorder_evidence import trajectory_path_for_gt
            traj_dir = trajectory_path_for_gt(gt_path).parent
            merge_observer_into_agent(agent, load_observer_trace(traj_dir))
        except Exception:
            pass  # never let the optional observer break scoring
    return agent, bool(records)


def nodes_in_group(nodes: list[dict], group: str) -> list[dict]:
    """The agent nodes belonging to one reasoning family (sources / root_causes / sinks)."""
    return [n for n in nodes if role_group(n.get("role")) == group]


def _eval_edges(step: dict, var_by_step: dict, agent: dict) -> list[dict[str, Any]]:
    to_var = str(step.get("var") or "")
    edges = []
    for dep in step.get("depends_on") or []:
        if not isinstance(dep, dict):
            continue
        etype = str(dep.get("type") or "data")
        src_var = str(var_by_step.get(dep.get("on"), "") or "")
        via = str(dep.get("via") or "")
        if etype == "data":
            # `via` is the value-carrying variable; match both endpoints.
            from_var = via or src_var
            hit = _match_data(from_var, to_var, agent["edges"])
            relation = None
        elif etype == "control":
            # `via` is a guard EXPRESSION; its operands (plus the step vars and `obj`)
            # are what must line up with the agent's recorded control edge.
            from_var = src_var
            relation = via
            operands = list(_ident_set(via)) + [src_var, to_var, str(dep.get("obj") or "")]
            hit = _match_relation("control", [o for o in operands if o], agent["edges"])
        else:
            # order: `via` is a relation keyword (a label); match on the ordered
            # OPERANDS: source var, sink var, `obj`.
            from_var = src_var
            relation = via
            operands = [o for o in (src_var, to_var, str(dep.get("obj") or "")) if o]
            hit = _match_relation("order", operands, agent["edges"])
        edge = {"from": from_var, "to": to_var, "type": etype,
                "status": "matched" if hit else "missing"}
        if relation:
            edge["relation"] = relation
        edges.append(edge)
    return edges


_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*")
_IDENT_STOP = {"sizeof", "null", "true", "false"}


def _ident_set(expr: str) -> set[str]:
    """Non-trivial identifier tokens (len >= 2, no operators/keywords/index vars)."""
    return {t for t in _IDENT_RE.findall((expr or "").lower()) if len(t) >= 2 and t not in _IDENT_STOP}


def _load_verified(gt_path) -> dict | None:
    """Stage 04's selected+verified invariants, written next to the GT."""
    vp = gt_path.parent / "verified_invariants.json"
    if not vp.exists():
        return None
    try:
        return json.loads(vp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _key_steps_from_verified(vi: dict) -> list[dict]:
    """Turn verified_invariants (flat nodes + edges) into fine_trace-like key steps
    (node + its incoming typed edges as depends_on) so the scoring logic is shared.
    Only nodes/edges 05 marked verified become hard invariants."""
    nodes = [n for n in (vi.get("nodes") or []) if isinstance(n, dict) and n.get("verified", True)]
    for i, n in enumerate(nodes, 1):
        n["_step"] = i
    # Canonical stage-04 schema: exactly ONE root_cause criterion at TOP level.
    top_crit = vi.get("root_cause_criterion")
    if not isinstance(top_crit, dict):
        top_crit = None
    var_to_step = {}
    for n in nodes:
        v = str(n.get("var") or "")
        if v:
            var_to_step.setdefault(v, n["_step"])
    edges = [e for e in (vi.get("edges") or []) if isinstance(e, dict) and e.get("verified", True)]
    steps = []
    for n in nodes:
        nv = str(n.get("var") or "")
        deps = []
        for e in edges:
            if not _sym_eq(str(e.get("to") or ""), nv):
                continue
            dep = {"type": str(e.get("type") or "data"), "via": str(e.get("via") or e.get("from") or "")}
            on = var_to_step.get(str(e.get("from") or ""))
            if on:
                dep["on"] = on
            if e.get("obj"):
                dep["obj"] = e["obj"]
            deps.append(dep)
        crit = top_crit if str(n.get("role") or "").lower() == "root_cause" else None
        steps.append({"step": n["_step"], "role": n.get("role"), "file": n.get("file"),
                      "function": n.get("function"), "line": n.get("line"), "var": nv,
                      "depends_on": deps, "criterion": crit})
    return steps


def _role_candidates(gt_role: str, nodes: list[dict]) -> list[dict]:
    """Agent nodes a GT key node of role `gt_role` may match: exact role, else same
    reasoning group (source / root_cause / sink family)."""
    gr = role_group(gt_role)
    role_l = str(gt_role or "").lower()
    return [
        n for n in nodes
        if str(n.get("role") or "").lower() == role_l
        or (gr is not None and role_group(n.get("role")) == gr)
    ]


def _eval_lifetime_order(criterion: dict, agent_edges: list[dict]) -> dict[str, Any]:
    """The required temporal edge for a lifetime root_cause: the agent must have recorded
    an `order` edge touching the freed object (any of its aliases across the sites)."""
    operands = [str(criterion.get("object") or "")]
    operands += [str(s.get("var") or "") for s in (criterion.get("sites") or [])]
    hit = _match_relation("order", [o for o in operands if o], agent_edges)
    return {"from": "", "to": str(criterion.get("object") or ""), "type": "order",
            "relation": criterion.get("relation"), "status": "matched" if hit else "missing"}


def _match_criterion(criterion: dict, candidates: list[dict]) -> dict[str, Any]:
    """Match a root_cause CRITERION rather than one line — class-aware:
      * lifetime: the agent names the freed/used OBJECT and sits at an alloc/free/use site;
      * bounds_check: the agent names the faulting VARIABLE and sits in the vulnerable region.
    Either way any valid framing of the same causal condition is credited (the condition/
    relation itself is checked via the node's typed edge in _eval_edges)."""
    if criterion.get("kind") == "lifetime":
        # Lifetime bugs: the alloc/free/use SITES are the invariant. Require the agent's
        # claim to be AT a site (function AND line within tolerance) — not merely somewhere
        # in the function. Pointer aliases vary, so the site line (not the object name) is
        # the position anchor; the temporal relation is required separately (see _eval_point).
        for c in candidates:
            cfn = str(c.get("function") or "")
            cline = _safe_int(c.get("line"))
            for s in criterion.get("sites") or []:
                sline = _safe_int(s.get("line"))
                if cfn == str(s.get("function") or "") and sline is not None and cline is not None and abs(cline - sline) <= LINE_TOLERANCE:
                    return {"status": "located", "agent": {"function": cfn, "line": cline, "var": c.get("var")}}
    else:
        var = str(criterion.get("variable") or "")
        fn = str(criterion.get("region_function") or "")
        region = criterion.get("region_lines") or []
        lo = _safe_int(region[0]) if len(region) >= 1 else None
        hi = _safe_int(region[1]) if len(region) >= 2 else None
        for c in candidates:
            var_ok = (not var) or _sym_eq(var, str(c.get("var") or ""))
            cfn_ok = (not fn) or str(c.get("function") or "") == fn
            cline = _safe_int(c.get("line"))
            region_ok = cfn_ok and (lo is None or hi is None or cline is None or (lo - LINE_TOLERANCE <= cline <= hi + LINE_TOLERANCE))
            if var_ok and region_ok:
                return {"status": "located", "agent": {"function": c.get("function"), "line": cline, "var": c.get("var")}}
    if candidates:
        c = candidates[0]
        return {"status": "wrong_location", "agent": {"function": c.get("function"), "line": _safe_int(c.get("line"))}}
    return {"status": "missing", "agent": None}


def _match_position(gt_pos: dict, candidates: list[dict], siblings: list = ()) -> dict[str, Any]:
    """Locate a GT point among agent candidates. Line proximity is the strong signal;
    a bare function match is accepted ONLY when no other key node shares this function
    (otherwise it cannot tell them apart). A line hit is rejected if the candidate sits
    closer to a same-function sibling than to this point."""
    gt_file = str(gt_pos.get("file") or "")
    gt_line = _safe_int(gt_pos.get("line"))
    gt_func = str(gt_pos.get("function") or "")
    confusable = [
        sl for (sf, sl) in siblings
        if sf == gt_func and sl is not None and gt_line is not None and sl != gt_line
    ]
    for c in candidates:
        cfile = str(c.get("file") or "")
        if not (path_suffix_matches(gt_file, cfile) or path_suffix_matches(cfile, gt_file)):
            continue
        cline = _safe_int(c.get("line"))
        if gt_line is not None and cline is not None and abs(gt_line - cline) <= LINE_TOLERANCE:
            if confusable and any(abs(cline - sl) < abs(cline - gt_line) for sl in confusable):
                continue  # this candidate belongs to a sibling point, not this one
            return {"status": "located", "agent": {"file": cfile, "function": c.get("function"), "line": cline}}
        if not confusable and gt_func and str(c.get("function") or "") == gt_func:
            return {"status": "located", "agent": {"file": cfile, "function": c.get("function"), "line": cline}}
    if candidates:
        c = candidates[0]
        return {"status": "wrong_location",
                "agent": {"file": c.get("file"), "function": c.get("function"), "line": _safe_int(c.get("line"))}}
    return {"status": "missing", "agent": None}


def _match_data(fv: str, tv: str, agent_edges: list[dict]) -> dict | None:
    """A data edge matches when both endpoints (provenance var -> target var) line up."""
    for e in agent_edges:
        if str(e.get("type") or "") != "data":
            continue
        if _sym_eq(fv, str(e.get("from") or "")) and _sym_eq(tv, str(e.get("to") or "")):
            return e
    return None


def _match_relation(etype: str, operands: list[str], agent_edges: list[dict]) -> dict | None:
    """A control/order edge matches when the agent recorded an edge of the same type
    touching any of the guarded/ordered operands (source var, sink var, or object)."""
    for e in agent_edges:
        if str(e.get("type") or "") != etype:
            continue
        ends = [str(e.get("from") or ""), str(e.get("to") or ""), str(e.get("obj") or "")]
        if any(_sym_eq(o, a) for o in operands for a in ends if a):
            return e
    return None


def _sym_eq(a: str, b: str) -> bool:
    """Two symbol references match when they normalize equal, or share a non-trivial
    identifier token — so `buf` matches `buf[i]`, but `s` does NOT match `status`."""
    na, nb = _norm_sym(a), _norm_sym(b)
    if na and na == nb:
        return True
    return bool(_ident_set(a) & _ident_set(b))


def _norm_sym(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", (s or "").lower().replace("->", "."))


def _precision(items: list, supported) -> float | None:
    items = [x for x in items if isinstance(x, dict)]
    if not items:
        return None
    return _ratio(sum(1 for x in items if supported(x)), len(items))


def _node_supported(node: dict, gt_nodes: list) -> bool:
    """An agent node is GT-supported when it lands (by file+line/function) on some GT
    fine_trace node of a compatible role group."""
    nf = str(node.get("file") or "")
    nl = _safe_int(node.get("line"))
    ng = role_group(node.get("role"))
    for gf, gfn, gl, grole in gt_nodes:
        if not (path_suffix_matches(gf, nf) or path_suffix_matches(nf, gf)):
            continue
        line_ok = nl is not None and gl is not None and abs(nl - gl) <= LINE_TOLERANCE
        func_ok = bool(gfn) and str(node.get("function") or "") == gfn
        role_ok = ng is None or role_group(grole) == ng
        if (line_ok or func_ok) and role_ok:
            return True
    return False


def _gt_edge_index(fine: list, var_by_step: dict) -> list[tuple[str, set]]:
    """Every GT edge as (type, operand-token-set) for precision matching."""
    out: list[tuple[str, set]] = []
    for s in fine:
        if not isinstance(s, dict):
            continue
        to_var = str(s.get("var") or "")
        for dep in s.get("depends_on") or []:
            if not isinstance(dep, dict):
                continue
            etype = str(dep.get("type") or "data")
            ops = _ident_set(str(dep.get("via") or "")) | _ident_set(to_var)
            ops |= _ident_set(str(var_by_step.get(dep.get("on"), "") or "")) | _ident_set(str(dep.get("obj") or ""))
            out.append((etype, ops))
    return out


def _edge_supported(edge: dict, gt_edges: list[tuple[str, set]]) -> bool:
    etype = str(edge.get("type") or "")
    ops = _ident_set(str(edge.get("from") or "")) | _ident_set(str(edge.get("to") or "")) | _ident_set(str(edge.get("obj") or ""))
    return any(etype == gt_type and (ops & gt_ops) for gt_type, gt_ops in gt_edges)


def _f1(recall: float | None, precision: float | None) -> float | None:
    if recall is None or precision is None or (recall + precision) == 0:
        return None
    return round(2 * recall * precision / (recall + precision), 4)


def _safe_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None
