"""T3 root-cause understanding evaluator (structured).

Distinct from t1 (endpoints) and invariant (between-chain): T3 isolates the
CAUSE-vs-SYMPTOM distinction — did the agent identify a root cause that is

  1. correctly LOCATED at the patch-fixed fault,
  2. DISTINCT from the crash point (not the naive "the crash line is the bug"), and
  3. causally LINKED to the crash (an edge from the fault toward the sink).

Scored from the agent's structured recorder claims, not trajectory text. The
causal-link check needs GT variables; when they are absent it degrades to the
position-only verdict rather than failing the sample.
"""

from __future__ import annotations

import json
from typing import Any

from .base import BaseEvaluator, EvaluationInput
from .invariant import _sym_eq, build_agent_state, nodes_in_group
from .trajectory import path_suffix_matches

LINE_TOLERANCE = 3


class T3RootCauseEvaluator(BaseEvaluator):
    name = "t3_root_cause"
    version = "0.3"

    def evaluate(self, inputs: EvaluationInput) -> dict[str, Any]:
        gt = json.loads(inputs.ground_truth.read_text(encoding="utf-8"))
        agent, has_records = build_agent_state(inputs.ground_truth)

        gt_root = _loc(gt.get("root_cause"))
        gt_sink = _loc(gt.get("sink"))
        crash = _loc((gt.get("sanitizer_ground_truth") or {}).get("crash_location")) or gt_sink

        claims = nodes_in_group(agent["nodes"], "root_causes")
        # Line-discriminative: when the fault and crash share a function, a function
        # match cannot tell them apart — require line proximity to the fault, closer
        # to it than to the crash.
        located = _locates(gt_root, gt_sink, claims)
        status = "located" if located else ("wrong_location" if _in_file(gt_root, claims) else "missing")

        # Is there a real cause/symptom distinction to test, and did the agent make it?
        gt_distinguishable = not _same_point(gt_root, gt_sink)
        at_symptom = _locates(gt_sink, gt_root, claims) or _locates(crash, gt_root, claims)
        distinguishes = gt_distinguishable and located
        mistook_crash_for_cause = gt_distinguishable and not located and at_symptom

        link = _cause_crash_link(_role_var(gt, "root_cause"), _role_var(gt, "sink"), agent["edges"])

        position_ok = located and (distinguishes or not gt_distinguishable)
        strict = position_ok and (link["strict"] if link["evaluable"] else True)
        lenient = located and (link["lenient"] if link["evaluable"] else True)

        return {
            "evaluator": self.name,
            "version": self.version,
            "structured_reasoning_evaluable": has_records,
            "inputs": {"ground_truth": str(inputs.ground_truth)},
            "summary": {
                "root_cause_status": status,
                "gt_distinguishable": gt_distinguishable,
                "distinguishes_cause_from_crash_symptom": distinguishes,
                "mistook_crash_for_cause": mistook_crash_for_cause,
                "cause_crash_link_evaluable": link["evaluable"],
                "cause_crash_link_seen": link["strict"],
                "strict_root_cause_understood": strict,
                "lenient_root_cause_understood": lenient,
            },
            "cause_crash_link": link,
        }


def _in_file(gt_pos: dict, claims: list[dict]) -> bool:
    gf = str(gt_pos.get("file") or "")
    return any(path_suffix_matches(gf, str(c.get("file") or "")) or
               path_suffix_matches(str(c.get("file") or ""), gf) for c in claims if c.get("file"))


def _locates(target: dict, other: dict, claims: list[dict]) -> bool:
    """Does any agent claim locate `target` while staying discriminative against
    `other`? Line proximity is required; a bare function match is accepted only when
    the function does not also contain `other` (otherwise it can't tell them apart)."""
    tf = str(target.get("file") or "")
    tl, ol = _safe_int(target.get("line")), _safe_int(other.get("line"))
    tfn, ofn = str(target.get("function") or ""), str(other.get("function") or "")
    same_func_diff_line = bool(tfn and tfn == ofn and tl is not None and ol is not None and tl != ol)
    for c in claims:
        cf = str(c.get("file") or "")
        if not (path_suffix_matches(tf, cf) or path_suffix_matches(cf, tf)):
            continue
        cl = _safe_int(c.get("line"))
        if cl is not None and tl is not None:
            if abs(cl - tl) <= LINE_TOLERANCE and not (
                same_func_diff_line and ol is not None and abs(cl - ol) < abs(cl - tl)
            ):
                return True
        elif not same_func_diff_line and tfn and str(c.get("function") or "") == tfn:
            return True
    return False


def _loc(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        return {}
    return {"file": obj.get("file"), "function": obj.get("function"), "line": obj.get("line")}


def _same_point(a: dict, b: dict) -> bool:
    """Two GT points are the same fault when same file and line within tolerance."""
    if not (a.get("file") and b.get("file")):
        return False
    if str(a["file"]) != str(b["file"]):
        return False
    la, lb = _safe_int(a.get("line")), _safe_int(b.get("line"))
    if la is not None and lb is not None:
        return abs(la - lb) <= LINE_TOLERANCE
    return str(a.get("function") or "") == str(b.get("function") or "")


def _role_var(gt: dict, role: str) -> str:
    for step in gt.get("fine_trace") or []:
        if isinstance(step, dict) and step.get("role") == role and step.get("var"):
            return str(step["var"])
    obj = gt.get(role)
    if isinstance(obj, dict) and obj.get("var"):
        return str(obj["var"])
    return ""


def _cause_crash_link(root_var: str, sink_var: str, agent_edges: list[dict]) -> dict[str, Any]:
    """Did the agent connect the fault to the crash? strict = one edge touches both
    the root-cause var and the sink var; lenient = the root-cause var participates in
    any recorded edge. Not evaluable without both GT variables."""
    if not (root_var and sink_var and agent_edges):
        return {"evaluable": False, "strict": False, "lenient": False}
    strict = lenient = False
    for e in agent_edges:
        ends = [str(e.get("from") or ""), str(e.get("to") or ""), str(e.get("obj") or "")]
        touches_root = any(_sym_eq(root_var, x) for x in ends if x)
        touches_sink = any(_sym_eq(sink_var, x) for x in ends if x)
        lenient = lenient or touches_root
        strict = strict or (touches_root and touches_sink)
    return {"evaluable": True, "strict": strict, "lenient": lenient}


def _safe_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
