#!/usr/bin/env python3
"""Strictly migrate complete GT artifacts to gt_contract shape.

This tool only writes samples whose relations can be derived from verified
assertions and field bindings. It refuses samples with missing required
assertions, unresolved operands, missing transition coverage, or empty assertion
sets. It never writes placeholder operands or relations.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORE = (
    "ground_truth.json",
    "verified_invariants.json",
    "verified_assertions.json",
    "field_bindings.json",
    "event_locations.json",
    "assertion_results.json",
)
OPS = {"eq", "ne", "lt", "le", "gt", "ge"}
LITERALS = {
    "null_literal": "NULL",
    "zero_literal": "0",
    "true_literal": "true",
    "false_literal": "false",
    "eof_literal": "XML_PARSER_EOF",
}
SINK_ASSERTION_OVERRIDES = {
    # These samples anchor the scored sink at the crashing callee/body, while
    # Stage 04 verified the nearest project callsite or enabling state. The
    # chosen assertion is the verified proof that binds the GT sink value.
    "arvo_17119": ("dims_aliases_stack_buffer", "A2"),
    "arvo_22110": ("E2", "A4"),
    "arvo_26524": ("n_libexif_call_uses_trimmed_data_ptr", "a_exif_blob_data_ptr_reaches_libexif_call"),
    "arvo_63314": ("node.raw_submit_window_overruns_empty", "transition.empty_extent_to_raw_submit_window"),
    "secbench_oss_openexr.ossfuzz-42524709": (
        "edge.root_unknown_start_reaches_unknown_zlib_call",
        "a_root_unknown_start_reaches_unknown_zlib_call",
    ),
}


class SkipSample(Exception):
    pass


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def dirs(root: Path) -> list[Path]:
    result = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("_") or ".repair-staging" in path.name:
            continue
        if all((path / name).is_file() for name in CORE[:-1]):
            result.append(path)
    return result


def binding_expr(bindings: dict[str, Any], key: str) -> str:
    value = bindings.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict) and isinstance(value.get("expr"), str) and value["expr"].strip():
        return value["expr"].strip()
    raise SkipSample(f"missing binding {key}")


def operand(value: Any, at: str, bindings: dict[str, Any]) -> str:
    if isinstance(value, str) and value.startswith("$"):
        raw_key = value[1:]
        field = raw_key.rsplit(".", 1)[-1]
        if field in LITERALS:
            return LITERALS[field]
        keys = [raw_key]
        if "." not in raw_key and at:
            keys.insert(0, f"{at}.{raw_key}")
        if field not in keys:
            keys.append(field)
        last_error = keys[0]
        for key in keys:
            try:
                return binding_expr(bindings, key)
            except SkipSample:
                last_error = key
        raise SkipSample(f"missing binding {last_error}")
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def relation(assertion: dict[str, Any], bindings: dict[str, Any]) -> dict[str, str]:
    check = assertion.get("check")
    if not isinstance(check, list) or len(check) != 3 or check[0] not in OPS:
        raise SkipSample(f"bad assertion check {assertion.get('id')}")
    left = operand(check[1], str(assertion.get("at") or ""), bindings)
    right = operand(check[2], str(assertion.get("at") or ""), bindings)
    op = str(check[0])
    if op in {"gt", "ge"}:
        op = {"gt": "lt", "ge": "le"}[op]
        left, right = right, left
    elif op in {"eq", "ne"} and right < left:
        left, right = right, left
    return {"op": op, "left": left, "right": right}


def operands_from_relation(rel: dict[str, str]) -> list[str]:
    result: list[str] = []
    for key in ("left", "right"):
        value = rel[key]
        if value not in result:
            result.append(value)
    return result


def anchor_operands(anchor: dict[str, Any]) -> list[str]:
    value = anchor.get("operands")
    if isinstance(value, list) and value:
        return [str(item) for item in value]
    value = anchor.get("var") or anchor.get("variable")
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    raise SkipSample("anchor has no operand")


def same_location(a: dict[str, Any], b: dict[str, Any], tolerance: int = 5) -> bool:
    try:
        return (
            str(a.get("file") or "") == str(b.get("file") or "")
            and str(a.get("function") or "") == str(b.get("function") or "")
            and abs(int(a.get("line")) - int(b.get("line"))) <= tolerance
        )
    except (TypeError, ValueError):
        return False


def assertion_by_invariant(assertions: list[dict[str, Any]], invariant_id: str, kind: str | None = None) -> dict[str, Any]:
    matches = [
        assertion for assertion in assertions
        if (kind is None or assertion.get("kind") == kind)
        and invariant_id in {str(item) for item in assertion.get("invariants", [])}
    ]
    if not matches:
        raise SkipSample(f"no {kind or ''} assertion for {invariant_id}")
    if kind == "transition" and len(matches) != 1:
        raise SkipSample(f"transition assertion count for {invariant_id}: {len(matches)}")
    return matches[0]


def has_assertion_for_invariant(assertions: list[dict[str, Any]], invariant_id: str, kind: str | None = None) -> bool:
    return any(
        (kind is None or assertion.get("kind") == kind)
        and invariant_id in {str(item) for item in assertion.get("invariants", [])}
        for assertion in assertions
    )


def is_omitted_unverified(item: dict[str, Any]) -> bool:
    status = str(item.get("verification_status") or item.get("status") or "").lower()
    if status.startswith("omitted") or "not_runtime_verified" in status:
        return True
    return item.get("verified") is False


def assertion_at_anchor(
    assertions: list[dict[str, Any]],
    locations: dict[str, Any],
    anchor: dict[str, Any],
    kinds: set[str],
) -> dict[str, Any]:
    for assertion in assertions:
        if assertion.get("kind") not in kinds:
            continue
        loc = locations.get(str(assertion.get("at") or ""))
        if isinstance(loc, dict) and same_location(loc, anchor):
            return assertion
    raise SkipSample(f"no assertion at anchor {anchor.get('function')}:{anchor.get('line')}")


def invariant_id_at_anchor(vi: dict[str, Any], anchor: dict[str, Any], role_hint: str | None = None) -> str | None:
    for node in vi.get("nodes", []) or []:
        if not isinstance(node, dict) or not node.get("invariant_id"):
            continue
        if role_hint and node.get("role") not in {role_hint, None, "sink", "root_cause"}:
            continue
        if same_location(node, anchor):
            return str(node["invariant_id"])
    return None


def sink_invariant_id(vi: dict[str, Any]) -> str | None:
    candidates = []
    for node in vi.get("nodes", []) or []:
        if not isinstance(node, dict) or not node.get("invariant_id"):
            continue
        role = str(node.get("role") or "").lower()
        typ = str(node.get("type") or "").lower()
        invariant_id = str(node.get("invariant_id") or "").lower()
        if role == "sink" or typ == "sink" or "sink" in invariant_id:
            candidates.append(str(node["invariant_id"]))
    return candidates[0] if len(candidates) == 1 else None


def sink_invariant_ids(vi: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for node in vi.get("nodes", []) or []:
        if not isinstance(node, dict) or not node.get("invariant_id"):
            continue
        role = str(node.get("role") or "").lower()
        typ = str(node.get("type") or "").lower()
        invariant_id_lower = str(node.get("invariant_id") or "").lower()
        if role == "sink" or typ == "sink" or "sink" in invariant_id_lower:
            candidates.append(str(node["invariant_id"]))
    return candidates


def assertion_for_sink_like_invariant(
    vi: dict[str, Any],
    assertions: list[dict[str, Any]],
    rcc_id: str,
) -> tuple[str, dict[str, Any]] | None:
    sink_ids = set(sink_invariant_ids(vi))
    if sink_ids:
        matches = [
            assertion for assertion in assertions
            if assertion.get("kind") in {"observed", "transition"}
            and sink_ids.intersection({str(item) for item in assertion.get("invariants", [])})
        ]
        if len(matches) == 1:
            sink_id = next(str(item) for item in matches[0].get("invariants", []) if str(item) in sink_ids)
            return sink_id, matches[0]

    # Some packages anchor the scored sink at a callee crash frame, while the
    # executable proof is the final verified transition into that callsite.
    # Use this only when there is a single non-root observed/transition proof,
    # so we do not guess among multiple possible sinks.
    matches = [
        assertion for assertion in assertions
        if assertion.get("kind") in {"observed", "transition"}
        and rcc_id not in {str(item) for item in assertion.get("invariants", [])}
    ]
    if len(matches) == 1 and matches[0].get("invariants"):
        return str(matches[0]["invariants"][0]), matches[0]
    return None


def assertion_for_manual_sink_override(
    sample_id: str,
    assertions: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    override = SINK_ASSERTION_OVERRIDES.get(sample_id)
    if not override:
        return None
    sink_id, assertion_id = override
    matches = [
        assertion for assertion in assertions
        if assertion.get("id") == assertion_id and sink_id in {str(item) for item in assertion.get("invariants", [])}
    ]
    if len(matches) != 1:
        raise SkipSample(f"sink override unresolved {sink_id} via {assertion_id}")
    return sink_id, matches[0]


def assertion_for_sink(
    vi: dict[str, Any],
    assertions: list[dict[str, Any]],
    locations: dict[str, Any],
    sink_anchor: dict[str, Any],
    rcc_id: str,
) -> tuple[str, dict[str, Any]]:
    # Many UAF/double-free GTs anchor the sink at the callee body where the
    # invalid read happens, while the runtime assertion event is at the callsite
    # that passes the stale object. Prefer the existing sink invariant id, then
    # fall back to event-location matching.
    sink_id = invariant_id_at_anchor(vi, sink_anchor, "sink") or sink_invariant_id(vi)
    if sink_id:
        matches = [
            assertion for assertion in assertions
            if assertion.get("kind") in {"observed", "transition"}
            and sink_id in {str(item) for item in assertion.get("invariants", [])}
        ]
        if matches:
            return sink_id, matches[0]
    assertion = assertion_at_anchor(assertions, locations, sink_anchor, {"observed", "transition"})
    sink_id = next((str(iid) for iid in assertion.get("invariants", []) if str(iid) != rcc_id), "N_SINK")
    return sink_id, assertion


def proof_gate(va: dict[str, Any], ar: dict[str, Any]) -> None:
    assertions = [item for item in va.get("assertions", []) if isinstance(item, dict)]
    if not assertions:
        raise SkipSample("no assertions")
    required = [item for item in assertions if item.get("kind") == "required"]
    if not required:
        raise SkipSample("no required assertion")
    if not ar:
        return
    by_id = {item.get("id"): item for item in ar.get("assertions", []) if isinstance(item, dict)}
    case = str(ar.get("original_case") or "original")
    for assertion in required:
        result = by_id.get(assertion.get("id")) or {}
        if result.get("verified") is not True:
            raise SkipSample(f"required not verified {assertion.get('id')}")
        vuln = (((result.get("matrix") or {}).get("vulnerable") or {}).get(case) or {})
        fixed = (((result.get("matrix") or {}).get("fixed") or {}).get(case) or {})
        if vuln.get("satisfied") is not False:
            raise SkipSample(f"required not refuted in vulnerable {assertion.get('id')}")
        if fixed.get("satisfied") is not True and fixed.get("status") not in {"guarded", "avoided", "not_exercised"}:
            raise SkipSample(f"required not satisfied/guarded in fixed {assertion.get('id')}")


def event_node(event: str, nodes: list[dict[str, Any]], locations: dict[str, Any]) -> str | None:
    loc = locations.get(event)
    if not isinstance(loc, dict):
        return None
    for node in nodes:
        if same_location(node, loc):
            return str(node["invariant_id"])
    return None


def ensure_event_node(
    event: str,
    *,
    nodes: list[dict[str, Any]],
    locations: dict[str, Any],
    relation_obj: dict[str, str],
    assertion_id: str,
) -> str:
    existing = event_node(event, nodes, locations)
    if existing:
        return existing
    loc = locations.get(event)
    if not isinstance(loc, dict) or not loc.get("file") or not loc.get("function") or not isinstance(loc.get("line"), int):
        raise SkipSample(f"missing event location {event}")
    invariant_id = f"N_EVENT_{event}"
    existing_ids = {str(node.get("invariant_id")) for node in nodes}
    if invariant_id in existing_ids:
        return invariant_id
    node = {
        "invariant_id": invariant_id,
        "role": "intermediate",
        "file": loc["file"],
        "function": loc["function"],
        "line": loc["line"],
        "operands": operands_from_relation(relation_obj),
        "relation": relation_obj,
        "verified": True,
        "verified_by": assertion_id,
        "description": f"Intermediate runtime event {event} materialized from verified transition assertion {assertion_id}.",
    }
    nodes.append(node)
    return invariant_id


def line_node(prefix: str, edge: dict[str, Any], nodes: list[dict[str, Any]]) -> str | None:
    loc = {
        "file": edge.get(f"{prefix}_file"),
        "function": edge.get(f"{prefix}_function"),
        "line": edge.get(f"{prefix}_line"),
    }
    for node in nodes:
        if same_location(node, loc):
            return str(node["invariant_id"])
    return None


def source_node(gt: dict[str, Any], invariant_id: str) -> dict[str, Any]:
    anchor = copy.deepcopy(gt["source"])
    ops = anchor_operands(anchor)
    node = {
        "invariant_id": invariant_id,
        "role": "source",
        "file": anchor["file"],
        "function": anchor["function"],
        "line": int(anchor["line"]),
        "operands": ops,
        "relation": {
            "op": "same_object",
            "left": ops[0],
            "right": ops[0],
            "description": "Source identity relation for the vulnerability-relevant value."
        },
        "verified": True,
    }
    if isinstance(anchor.get("trace_step"), int):
        node["fine_trace_step"] = anchor["trace_step"]
    if anchor.get("description"):
        node["description"] = anchor["description"]
    return node


def anchor_node(role: str, invariant_id: str, anchor: dict[str, Any], rel: dict[str, str], verified_by: str) -> dict[str, Any]:
    node = {
        "invariant_id": invariant_id,
        "role": role,
        "file": anchor["file"],
        "function": anchor["function"],
        "line": int(anchor["line"]),
        "operands": operands_from_relation(rel),
        "relation": rel,
        "verified": True,
        "verified_by": verified_by,
    }
    if isinstance(anchor.get("trace_step"), int):
        node["fine_trace_step"] = anchor["trace_step"]
    if anchor.get("description"):
        node["description"] = anchor["description"]
    return node


def normalize_existing_node(old: dict[str, Any], assertions: list[dict[str, Any]], bindings: dict[str, Any]) -> dict[str, Any]:
    iid = str(old.get("invariant_id") or "")
    assertion = assertion_by_invariant(assertions, iid)
    rel = relation(assertion, bindings)
    node = copy.deepcopy(old)
    node["role"] = node.get("role") if node.get("role") in {"source", "root_cause", "sink", "intermediate"} else "intermediate"
    node["operands"] = operands_from_relation(rel)
    node["relation"] = rel
    node["verified"] = True
    node["verified_by"] = node.get("verified_by") or assertion.get("id")
    node.pop("variable", None)
    node.pop("var", None)
    return node


def migrate(result_dir: Path) -> dict[str, dict[str, Any]]:
    gt = load(result_dir / "ground_truth.json")
    vi = load(result_dir / "verified_invariants.json")
    va = load(result_dir / "verified_assertions.json")
    fb = load(result_dir / "field_bindings.json")
    el = load(result_dir / "event_locations.json")
    ar = load(result_dir / "assertion_results.json") if (result_dir / "assertion_results.json").is_file() else {}
    proof_gate(va, ar)
    bindings = fb.get("bindings") if isinstance(fb.get("bindings"), dict) else {}
    locations = el.get("locations") if isinstance(el.get("locations"), dict) else {}
    assertions = [item for item in va.get("assertions", []) if isinstance(item, dict)]
    rcc = vi.get("root_cause_criterion") if isinstance(vi.get("root_cause_criterion"), dict) else {}
    rcc_id = str(rcc.get("invariant_id") or "")
    if not rcc_id:
        raise SkipSample("missing root_cause_criterion id")

    root_assertion = assertion_by_invariant(assertions, rcc_id, "required")
    root_rel = relation(root_assertion, bindings)
    try:
        sink_id, sink_assertion = assertion_for_sink(vi, assertions, locations, gt["sink"], rcc_id)
    except SkipSample:
        sink_like = (
            assertion_for_manual_sink_override(result_dir.name, assertions)
            or assertion_for_sink_like_invariant(vi, assertions, rcc_id)
        )
        if not sink_like:
            raise
        sink_id, sink_assertion = sink_like
    sink_rel = relation(sink_assertion, bindings)

    new_gt = copy.deepcopy(gt)
    new_gt.pop("coarse_trace", None)
    new_gt["source"]["operands"] = anchor_operands(new_gt["source"])
    src_op = new_gt["source"]["operands"][0]
    new_gt["source"]["relation"] = {
        "op": "same_object",
        "left": src_op,
        "right": src_op,
        "description": "Source identity relation for the vulnerability-relevant value."
    }
    new_gt["root_cause"]["operands"] = operands_from_relation(root_rel)
    new_gt["root_cause"]["relation"] = root_rel
    new_gt["sink"]["operands"] = operands_from_relation(sink_rel)
    new_gt["sink"]["relation"] = sink_rel
    if isinstance(new_gt.get("tainted_value_origin"), dict):
        new_gt["tainted_value_origin"]["operands"] = anchor_operands(new_gt["tainted_value_origin"])

    source_id = invariant_id_at_anchor(vi, gt["source"], "source") or "N_SOURCE"
    nodes = [
        source_node(new_gt, source_id),
        anchor_node("root_cause", rcc_id, new_gt["root_cause"], root_rel, str(root_assertion.get("id"))),
        anchor_node("sink", sink_id, new_gt["sink"], sink_rel, str(sink_assertion.get("id"))),
    ]
    seen = {node["invariant_id"] for node in nodes}
    for old in vi.get("nodes", []) or []:
        if not isinstance(old, dict) or not old.get("invariant_id"):
            continue
        if str(old["invariant_id"]) in seen:
            continue
        node = normalize_existing_node(old, assertions, bindings)
        if same_location(node, new_gt["sink"]):
            node["role"] = "sink"
        elif same_location(node, new_gt["root_cause"]):
            node["role"] = "root_cause"
        nodes.append(node)
        seen.add(str(old["invariant_id"]))

    edges = []
    for old in vi.get("edges", []) or []:
        if not isinstance(old, dict) or not old.get("invariant_id"):
            continue
        eid = str(old["invariant_id"])
        if not has_assertion_for_invariant(assertions, eid, "transition") and is_omitted_unverified(old):
            continue
        assertion = assertion_by_invariant(assertions, eid, "transition")
        rel = relation(assertion, bindings)
        edge = copy.deepcopy(old)
        etype = edge.get("type") if edge.get("type") in {"data", "control", "order"} else "data"
        edge["type"] = etype
        from_node = (
            (edge.get("from_node") if edge.get("from_node") in seen else None)
            or event_node(str(assertion.get("from") or ""), nodes, locations)
            or line_node("from", old, nodes)
        )
        to_node = (
            (edge.get("to_node") if edge.get("to_node") in seen else None)
            or event_node(str(assertion.get("at") or ""), nodes, locations)
            or line_node("to", old, nodes)
        )
        if not from_node or not to_node:
            # Transition endpoints are runtime events. If no selected invariant
            # node currently sits at the event location, materialize a verified
            # intermediate node from the same assertion/location instead of
            # guessing from prose.
            from_event = str(assertion.get("from") or "")
            to_event = str(assertion.get("at") or "")
            if not from_node and from_event:
                from_node = ensure_event_node(
                    from_event,
                    nodes=nodes,
                    locations=locations,
                    relation_obj=rel,
                    assertion_id=str(assertion.get("id") or ""),
                )
            if not to_node and to_event:
                to_node = ensure_event_node(
                    to_event,
                    nodes=nodes,
                    locations=locations,
                    relation_obj=rel,
                    assertion_id=str(assertion.get("id") or ""),
                )
        if not from_node or not to_node:
            raise SkipSample(f"edge endpoint unresolved {eid}")
        edge["from_node"] = from_node
        edge["to_node"] = to_node
        edge["operands"] = operands_from_relation(rel)
        edge["relation"] = rel
        edge["verified"] = True
        edge["verified_by"] = edge.get("verified_by") or assertion.get("id")
        for drift_key in (
            "via",
            "from",
            "to",
            "from_event",
            "to_event",
            "from_variable",
            "to_variable",
        ):
            edge.pop(drift_key, None)
        edges.append(edge)

    new_vi = copy.deepcopy(vi)
    new_vi.pop("schema_version", None)
    new_vi["nodes"] = nodes
    new_vi["edges"] = edges
    new_vi["root_cause_criterion"] = {"invariant_id": rcc_id}

    new_va = copy.deepcopy(va)
    new_va.pop("schema_version", None)
    new_fb = copy.deepcopy(fb)
    new_fb.pop("schema_version", None)
    for key, value in list((new_fb.get("bindings") or {}).items()):
        if isinstance(value, str):
            new_fb["bindings"][key] = {"expr": value, "aliases": [value]}
    new_el = copy.deepcopy(el)
    new_el.pop("schema_version", None)
    return {
        "ground_truth.json": new_gt,
        "verified_invariants.json": new_vi,
        "verified_assertions.json": new_va,
        "field_bindings.json": new_fb,
        "event_locations.json": new_el,
    }


def validate_contract(artifacts: dict[str, dict[str, Any]]) -> None:
    gt = artifacts["ground_truth.json"]
    vi = artifacts["verified_invariants.json"]
    va = artifacts["verified_assertions.json"]
    for name in ("ground_truth.json", "verified_invariants.json", "verified_assertions.json", "field_bindings.json", "event_locations.json"):
        if "schema_version" in artifacts[name]:
            raise SkipSample(f"{name} still has schema_version")
    if "coarse_trace" in gt:
        raise SkipSample("coarse_trace still present")
    for key in ("source", "root_cause", "sink"):
        if not gt[key].get("operands"):
            raise SkipSample(f"{key} missing operands")
    for key in ("root_cause", "sink"):
        rel = gt[key].get("relation")
        if not isinstance(rel, dict) or not all(k in rel for k in ("op", "left", "right")):
            raise SkipSample(f"{key} missing relation")
    node_ids = {str(node.get("invariant_id")) for node in vi.get("nodes", [])}
    roles = {node.get("role") for node in vi.get("nodes", [])}
    if not {"source", "root_cause", "sink"} <= roles:
        raise SkipSample("missing required node roles")
    if vi.get("root_cause_criterion", {}).get("invariant_id") not in node_ids:
        raise SkipSample("root criterion not materialized")
    edge_ids = {str(edge.get("invariant_id")) for edge in vi.get("edges", [])}
    for node in vi.get("nodes", []):
        if not node.get("operands") or not isinstance(node.get("relation"), dict):
            raise SkipSample(f"bad node {node.get('invariant_id')}")
    for edge in vi.get("edges", []):
        if edge.get("from_node") not in node_ids or edge.get("to_node") not in node_ids:
            raise SkipSample(f"bad edge endpoints {edge.get('invariant_id')}")
        if not edge.get("operands") or not isinstance(edge.get("relation"), dict):
            raise SkipSample(f"bad edge relation {edge.get('invariant_id')}")
    selected = node_ids | edge_ids
    for assertion in va.get("assertions", []):
        for invariant_id in assertion.get("invariants", []):
            if str(invariant_id) not in selected:
                raise SkipSample(f"assertion FK missing {assertion.get('id')} -> {invariant_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gt-root", type=Path, default=ROOT / "gt_results")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--report", type=Path, default=ROOT / "gt_contract_strict_migration_report.json")
    args = parser.parse_args()
    targets = [args.gt_root / sid for sid in args.sample_id] if args.sample_id else dirs(args.gt_root)
    reports = []
    for result_dir in targets:
        try:
            artifacts = migrate(result_dir)
            validate_contract(artifacts)
            if args.apply:
                for name, artifact in artifacts.items():
                    dump(result_dir / name, artifact)
            reports.append({"sample_id": result_dir.name, "status": "migrated"})
        except SkipSample as exc:
            reports.append({"sample_id": result_dir.name, "status": "skipped", "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001
            reports.append({"sample_id": result_dir.name, "status": "error", "reason": f"{type(exc).__name__}: {exc}"})
    summary = {
        "apply": args.apply,
        "total": len(reports),
        "migrated": sum(1 for item in reports if item["status"] == "migrated"),
        "skipped": sum(1 for item in reports if item["status"] == "skipped"),
        "errors": sum(1 for item in reports if item["status"] == "error"),
        "reports": reports,
    }
    args.report.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("apply", "total", "migrated", "skipped", "errors")}, indent=2))
    print(args.report)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
