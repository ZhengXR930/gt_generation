"""Freeze, execute, and verify vulnerability reasoning assertions.

The GT generator emits typed assertions and runtime traces verify them. Question
wording is deliberately handled later by the constrained questioning agent.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import operator
import re
from pathlib import Path
from typing import Any


_CASE_RE = re.compile(r"^CASE name=(\S+) rc=(-?\d+) result=(\S+)$")
_EVENT_RE = re.compile(r"^ASSERT_EVT\s+(.*)$")
_OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}
_SYMBOLS = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
_MISSING = object()


def assertion_content_hash(spec: dict[str, Any]) -> str:
    payload = dict(spec)
    payload.pop("content_hash", None)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_frozen_spec(spec: dict[str, Any]) -> None:
    schema_version = spec.get("schema_version")
    if schema_version not in {"assertion-spec-v2", "assertion-spec-v3"}:
        raise ValueError("expected assertion-spec-v2 or assertion-spec-v3")
    strict_edges = schema_version == "assertion-spec-v3"
    declared = str(spec.get("content_hash") or "")
    actual = assertion_content_hash(spec)
    if declared != actual:
        raise ValueError(
            f"assertion content hash mismatch: declared={declared!r} actual={actual!r}"
        )
    seen: set[str] = set()
    for assertion in spec.get("assertions", []):
        assertion_id = str(assertion.get("id") or "")
        if not assertion_id or assertion_id in seen:
            raise ValueError(f"missing or duplicate assertion id: {assertion_id!r}")
        seen.add(assertion_id)
        allowed_kinds = (
            {"observed", "required", "transition"}
            if strict_edges
            else {"observed", "required"}
        )
        if assertion.get("kind") not in allowed_kinds:
            raise ValueError(f"invalid assertion kind: {assertion.get('kind')!r}")
        check = assertion.get("check")
        if not isinstance(check, list) or len(check) != 3 or check[0] not in _OPS:
            raise ValueError(f"invalid check for {assertion_id}: {check!r}")
        if assertion.get("kind") in {"observed", "transition"} and not all(
            isinstance(operand, str) and operand.startswith("$")
            for operand in check[1:]
        ):
            raise ValueError(
                f"{assertion.get('kind')} assertion {assertion_id} must relate two source-derived "
                "event fields; runtime literals are not valid reasoning answers"
            )
        if not assertion.get("at"):
            raise ValueError(f"assertion {assertion_id} is missing at")
        if assertion.get("kind") == "transition" and not assertion.get("from"):
            raise ValueError(f"transition assertion {assertion_id} is missing from")
        if assertion.get("kind") == "transition":
            source_prefix = f"${assertion['from']}."
            target_prefix = f"${assertion['at']}."
            operands = check[1:]
            if not any(
                isinstance(operand, str) and operand.startswith(source_prefix)
                for operand in operands
            ) or not any(
                isinstance(operand, str) and operand.startswith(target_prefix)
                for operand in operands
            ):
                raise ValueError(
                    f"transition assertion {assertion_id} must directly relate one "
                    f"{assertion['from']} field to one {assertion['at']} field"
                )
        invariants = assertion.get("invariants")
        if not isinstance(invariants, list) or not invariants:
            raise ValueError(f"assertion {assertion_id} has no invariants")


def freeze_spec(spec_path: Path, marker_path: Path) -> dict[str, Any]:
    """Validate and persist the immutable pre-execution assertion commitment."""
    spec_path = spec_path.resolve()
    spec_bytes = spec_path.read_bytes()
    spec = json.loads(spec_bytes)
    validate_frozen_spec(spec)
    marker = {
        "schema_version": "assertion-freeze-v1",
        "spec_path": str(spec_path),
        "content_hash": spec["content_hash"],
        "file_sha256": "sha256:" + hashlib.sha256(spec_bytes).hexdigest(),
        "frozen_at": datetime.now(timezone.utc).isoformat(),
    }
    marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return marker


def validate_invariant_bindings(
    verified_invariants: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any]:
    """Require every selected reasoning invariant to have assertion coverage."""
    selected: set[str] = set()
    errors: list[str] = []
    nodes = [
        item
        for item in verified_invariants.get("nodes", [])
        if isinstance(item, dict)
    ]
    edges = [
        item
        for item in verified_invariants.get("edges", [])
        if isinstance(item, dict)
    ]
    criterion = verified_invariants.get("root_cause_criterion")
    edge_ids = {
        str(item.get("invariant_id")) for item in edges if item.get("invariant_id")
    }
    strict_edges = spec.get("schema_version") == "assertion-spec-v3"
    sections = [("node", item) for item in nodes] + [("edge", item) for item in edges]
    if isinstance(criterion, dict):
        sections.append(("root", criterion))
    if "refuted" in verified_invariants:
        errors.append("verified_invariants.json must not contain a refuted section")
    for section, item in sections:
        if "verified" in item and item.get("verified") is not True:
            errors.append(
                f"{section} invariant {item.get('invariant_id', '<missing>')} is not verified"
            )
        invariant_id = str(item.get("invariant_id") or "").strip()
        if not invariant_id:
            errors.append("selected invariant is missing invariant_id")
        elif invariant_id in selected:
            errors.append(f"duplicate invariant_id: {invariant_id}")
        else:
            selected.add(invariant_id)

    covered: set[str] = set()
    edge_transition_coverage: dict[str, list[str]] = {
        invariant_id: [] for invariant_id in edge_ids
    }
    for assertion in spec.get("assertions", []):
        assertion_id = assertion.get("id", "<missing>")
        assertion_invariants = {
            str(invariant_id) for invariant_id in assertion.get("invariants", [])
        }
        assertion_edges = sorted(assertion_invariants & edge_ids)
        if strict_edges:
            if assertion.get("kind") == "transition":
                if len(assertion_edges) != 1:
                    errors.append(
                        f"transition assertion {assertion_id} must cover exactly one edge; "
                        f"got {assertion_edges}"
                    )
                else:
                    edge_transition_coverage[assertion_edges[0]].append(str(assertion_id))
            elif assertion_edges:
                errors.append(
                    f"non-transition assertion {assertion_id} cannot cover edges: "
                    + ", ".join(assertion_edges)
                )
        for invariant_id in assertion.get("invariants", []):
            if invariant_id not in selected:
                errors.append(
                    f"assertion {assertion_id} references unknown invariant {invariant_id}"
                )
            else:
                covered.add(invariant_id)
    for invariant_id in sorted(selected - covered):
        errors.append(f"invariant {invariant_id} has no assertion")
    if strict_edges:
        for invariant_id, assertion_ids in sorted(edge_transition_coverage.items()):
            if len(assertion_ids) != 1:
                errors.append(
                    f"edge {invariant_id} requires exactly one transition assertion; "
                    f"got {assertion_ids}"
                )
    return {
        "valid": not errors,
        "invariant_count": len(selected),
        "assertion_count": len(spec.get("assertions", [])),
        "errors": errors,
    }


def parse_trace_matrix(text: str) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = _CASE_RE.match(line)
        if match:
            name, rc, result = match.groups()
            current = {"rc": int(rc), "result": result, "events": []}
            cases[name] = current
            continue
        match = _EVENT_RE.match(line)
        if match and current is not None:
            current["events"].append(_parse_fields(match.group(1)))
        elif line == "ENDCASE":
            current = None
    return cases


def _parse_fields(raw: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for token in raw.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if value == "(nil)":
            fields[key] = None
            continue
        try:
            fields[key] = int(value, 0)
        except ValueError:
            fields[key] = value
    return fields


def _events(case: dict[str, Any], point: str) -> list[dict[str, Any]]:
    return [event for event in case["events"] if event.get("point") == point]


def _event_with_index(
    case: dict[str, Any], point: str, *, last: bool = True
) -> tuple[int, dict[str, Any]] | None:
    matches = [
        (index, event)
        for index, event in enumerate(case["events"])
        if event.get("point") == point
    ]
    if not matches:
        return None
    return matches[-1] if last else matches[0]


def _preceding_event_with_index(
    case: dict[str, Any], point: str, target_index: int
) -> tuple[int, dict[str, Any]] | None:
    for index in range(target_index, -1, -1):
        event = case["events"][index]
        if event.get("point") == point:
            return index, event
    return None


def _transition_event_pair(
    case: dict[str, Any], source_point: str, target_point: str
) -> tuple[tuple[int, dict[str, Any]], tuple[int, dict[str, Any]]] | None:
    """Pair the latest usable source with its first subsequent target.

    A parser trace often repeats one event point for several records or I/O calls.
    Taking the last target in the entire case can therefore cross a call boundary.
    """
    sources = [
        (index, event)
        for index, event in enumerate(case["events"])
        if event.get("point") == source_point
    ]
    targets = [
        (index, event)
        for index, event in enumerate(case["events"])
        if event.get("point") == target_point
    ]
    for source in reversed(sources):
        target = next((item for item in targets if item[0] > source[0]), None)
        if target is not None:
            return source, target
    return None


def _operand(
    value: Any,
    *,
    case: dict[str, Any],
    default_event: dict[str, Any],
    bound_events: dict[str, dict[str, Any]] | None = None,
) -> Any:
    if not (isinstance(value, str) and value.startswith("$")):
        return value
    reference = value[1:]
    if "." not in reference:
        return default_event.get(reference, _MISSING)
    point, field = reference.split(".", 1)
    if bound_events and point in bound_events:
        return bound_events[point].get(field, _MISSING)
    located = _event_with_index(case, point)
    if located is None:
        return _MISSING
    return located[1].get(field, _MISSING)


def _check(
    assertion: dict[str, Any],
    case: dict[str, Any],
    event: dict[str, Any],
    *,
    bound_events: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, Any, Any, str] | None:
    op_name, left_ref, right_ref = assertion["check"]
    left = _operand(
        left_ref,
        case=case,
        default_event=event,
        bound_events=bound_events,
    )
    right = _operand(
        right_ref,
        case=case,
        default_event=event,
        bound_events=bound_events,
    )
    if left is _MISSING or right is _MISSING:
        return None
    return _OPS[op_name](left, right), left, right, op_name


def evaluate_assertion(
    assertion: dict[str, Any], case: dict[str, Any], version: str
) -> dict[str, Any]:
    del version  # The same assertion is executed against both versions.
    transition_meta: dict[str, Any] = {}
    bound_events: dict[str, dict[str, Any]] | None = None
    if assertion["kind"] == "transition":
        pair = _transition_event_pair(case, assertion["from"], assertion["at"])
        if pair is None:
            any_source = _event_with_index(case, assertion["from"])
            any_target = _event_with_index(case, assertion["at"])
            if any_source is not None and any_target is not None:
                return {
                    "from": assertion["from"],
                    "to": assertion["at"],
                    "from_index": any_source[0],
                    "to_index": any_target[0],
                    "ordered": False,
                    "status": "out_of_order",
                    "satisfied": False,
                }
            return {"status": "not_exercised", "satisfied": None}
        source, target = pair
        source_index, source_event = source
        target_index, target_event = target
        transition_meta = {
            "from": assertion["from"],
            "to": assertion["at"],
            "from_index": source_index,
            "to_index": target_index,
            "ordered": True,
        }
        bound_events = {
            assertion["from"]: source_event,
            assertion["at"]: target_event,
        }
    else:
        target = _event_with_index(case, assertion["at"])
        if target is None:
            return {"status": "not_exercised", "satisfied": None}
        target_index, target_event = target
    checked = _check(
        assertion,
        case,
        target_event,
        bound_events=bound_events,
    )
    if checked is None:
        return {**transition_meta, "status": "not_exercised", "satisfied": None}
    satisfied, left, right, op_name = checked
    result = {"left": left, "right": right, "op": op_name}
    protected = assertion.get("protects")
    if assertion["kind"] == "required" and protected:
        triggered = bool(_events(case, protected))
        if triggered:
            status = "genuine" if satisfied else "violated"
        else:
            status = "avoided" if satisfied else "guarded"
        return {
            **result,
            "status": status,
            "satisfied": status in {"genuine", "guarded"},
            "triggered": triggered,
        }
    status = "satisfied" if satisfied else "refuted"
    return {**transition_meta, **result, "status": status, "satisfied": satisfied}


def validate_assertions(
    spec: dict[str, Any],
    vulnerable_cases: dict[str, dict[str, Any]],
    fixed_cases: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    validate_frozen_spec(spec)
    original = spec.get("original_case", "original")
    results: list[dict[str, Any]] = []
    for assertion in spec.get("assertions", []):
        matrix = {
            version: {
                name: evaluate_assertion(assertion, case, version)
                for name, case in cases.items()
            }
            for version, cases in (
                ("vulnerable", vulnerable_cases),
                ("fixed", fixed_cases),
            )
        }
        vulnerable_status = matrix["vulnerable"].get(original, {}).get("status")
        fixed_status = matrix["fixed"].get(original, {}).get("status")
        if assertion["kind"] in {"observed", "transition"}:
            verified = vulnerable_status == "satisfied"
        elif assertion.get("protects"):
            genuine_witness_case = None
            if fixed_status == "guarded":
                genuine_witness_case = next(
                    (
                        name
                        for name, outcome in matrix["fixed"].items()
                        if name != original and outcome.get("status") == "genuine"
                    ),
                    None,
                )
            verified = vulnerable_status == "violated" and (
                fixed_status == "genuine"
                or (fixed_status == "guarded" and genuine_witness_case is not None)
            )
        else:
            verified = vulnerable_status == "refuted" and fixed_status == "satisfied"
        item = {
            "id": assertion["id"],
            "kind": assertion["kind"],
            "verified": verified,
            "matrix": matrix,
        }
        if assertion.get("protects") and fixed_status == "guarded":
            item["genuine_witness_case"] = genuine_witness_case
            if genuine_witness_case is None:
                item["verification_error"] = (
                    "fixed original is guarded; add a perturbation that executes the "
                    "protected event while the required predicate is true"
                )
        results.append(item)
    return {
        "schema_version": (
            "assertion-results-v3"
            if spec.get("schema_version") == "assertion-spec-v3"
            else "assertion-results-v2"
        ),
        "sample_id": spec.get("sample_id"),
        "candidate_content_hash": spec.get("content_hash"),
        "original_case": original,
        "all_verified": bool(results) and all(item["verified"] for item in results),
        "assertions": results,
    }


def build_verified_assertions(
    spec: dict[str, Any], results: dict[str, Any]
) -> dict[str, Any]:
    verified = {item["id"] for item in results.get("assertions", []) if item["verified"]}
    return {
        "schema_version": (
            "verified-assertions-v3"
            if spec.get("schema_version") == "assertion-spec-v3"
            else "verified-assertions-v2"
        ),
        "sample_id": spec.get("sample_id"),
        "content_hash": spec.get("content_hash"),
        "assertions": [item for item in spec.get("assertions", []) if item["id"] in verified],
    }


def build_perturbation_results(
    spec: dict[str, Any], results: dict[str, Any]
) -> dict[str, Any]:
    """Summarize why perturbation was needed and which cases were informative."""
    original = str(results.get("original_case") or spec.get("original_case") or "original")
    required = [
        item for item in results.get("assertions", [])
        if item.get("kind") == "required" and item.get("matrix")
    ]
    needed_ids = [
        str(item["id"])
        for item in required
        if item["matrix"].get("fixed", {}).get(original, {}).get("status") == "guarded"
    ]
    case_names = list(dict.fromkeys(
        name
        for item in required
        for version in ("vulnerable", "fixed")
        for name in item["matrix"].get(version, {})
        if name != original
    ))
    cases = []
    for name in case_names:
        assertions = []
        for item in required:
            vulnerable = item["matrix"].get("vulnerable", {}).get(name, {})
            fixed = item["matrix"].get("fixed", {}).get(name, {})
            useful = str(item["id"]) in needed_ids and fixed.get("status") == "genuine"
            assertions.append({
                "id": item["id"],
                "vulnerable_status": vulnerable.get("status", "missing"),
                "fixed_status": fixed.get("status", "missing"),
                "fixed_protected_event": fixed.get("triggered"),
                "useful": useful,
            })
        cases.append({"name": name, "assertions": assertions})
    witnesses = {
        str(item["id"]): item.get("genuine_witness_case")
        for item in required
        if str(item["id"]) in needed_ids
    }
    all_needed_witnessed = all(witnesses.get(assertion_id) for assertion_id in needed_ids)
    return {
        "schema_version": "perturbation-results-v1",
        "sample_id": spec.get("sample_id"),
        "needed": bool(needed_ids),
        "reason": (
            "fixed original was guarded for: " + ", ".join(needed_ids)
            if needed_ids else
            "fixed original did not leave a guarded required assertion"
        ),
        "cases": cases,
        "genuine_witness_cases": witnesses,
        "all_needed_witnessed": all_needed_witnessed,
    }


def _format_operand(value: Any) -> str:
    if isinstance(value, str) and value.startswith("$"):
        return value[1:]
    return json.dumps(value, ensure_ascii=False)


def format_check(check: list[Any]) -> str:
    op_name, left, right = check
    return f"{_format_operand(left)} {_SYMBOLS[op_name]} {_format_operand(right)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--vulnerable-trace", type=Path)
    parser.add_argument("--fixed-trace", type=Path)
    parser.add_argument("--results-out", type=Path)
    parser.add_argument("--verified-invariants", type=Path)
    parser.add_argument("--verified-assertions-out", type=Path)
    parser.add_argument("--perturbation-results-out", type=Path)
    parser.add_argument("--check-bindings-only", action="store_true")
    parser.add_argument("--freeze-only", action="store_true")
    parser.add_argument("--freeze-marker", type=Path)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    validate_frozen_spec(spec)
    if args.freeze_only:
        if not args.freeze_marker:
            parser.error("--freeze-only requires --freeze-marker")
        print(json.dumps(freeze_spec(args.spec, args.freeze_marker), indent=2))
        return 0
    if args.check_bindings_only:
        if not args.verified_invariants:
            parser.error("--check-bindings-only requires --verified-invariants")
        binding = validate_invariant_bindings(
            json.loads(args.verified_invariants.read_text(encoding="utf-8")), spec
        )
        print(json.dumps(binding, indent=2))
        if not binding["valid"]:
            raise SystemExit(1)
        return 0
    if not args.vulnerable_trace or not args.fixed_trace or not args.results_out:
        parser.error(
            "validation requires --vulnerable-trace, --fixed-trace, and --results-out"
        )
    results = validate_assertions(
        spec,
        parse_trace_matrix(args.vulnerable_trace.read_text(encoding="utf-8")),
        parse_trace_matrix(args.fixed_trace.read_text(encoding="utf-8")),
    )
    if args.verified_invariants:
        results["invariant_binding"] = validate_invariant_bindings(
            json.loads(args.verified_invariants.read_text(encoding="utf-8")), spec
        )
        if not results["invariant_binding"]["valid"]:
            raise ValueError("invalid invariant bindings")
    args.results_out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    if args.verified_assertions_out:
        args.verified_assertions_out.write_text(
            json.dumps(build_verified_assertions(spec, results), indent=2) + "\n",
            encoding="utf-8",
        )
    if args.perturbation_results_out:
        args.perturbation_results_out.write_text(
            json.dumps(build_perturbation_results(spec, results), indent=2) + "\n",
            encoding="utf-8",
        )
    if not results["all_verified"]:
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
