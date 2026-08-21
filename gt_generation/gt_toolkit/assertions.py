"""Freeze, execute, and verify vulnerability reasoning assertions.

The GT generator emits typed assertions and runtime traces verify them. Question
wording is deliberately handled later by the constrained questioning agent.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import operator
import re
import sys
from pathlib import Path
from typing import Any

from evaluator.field_bindings import binding_expr


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
_FIXED_GUARD_STATUSES = {"guarded", "avoided"}
_SYMBOLS = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
_MISSING = object()
# MSAN prints the poison boundary as e.g.
#   Uninitialized bytes in __interceptor_memcmp at offset 2 inside [0x7ffc.., 7)
# offset N => bytes 0..N-1 of the read are defined, byte N is the first poisoned
# one, so N is the sanitizer-authoritative initialized-prefix length; S is the
# read/compare size.
_MSAN_UNINIT_RE = re.compile(
    r"[Uu]ninitialized bytes? in \S+ at offset (\d+) inside \[(?:0x)?[0-9a-fA-F]+,\s*(\d+)\)"
)


def _load_assertion_reward_module():
    module_name = "_gt_assertion_reward"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = Path(__file__).resolve().parents[2] / "reward_framework" / "assertion_reward.py"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load assertion reward module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def parse_msan_uninit(sanitizer_text: str) -> dict[str, int] | None:
    """Return the MSAN use-of-uninitialized-value boundary, or None.

    {'init_prefix_len': N, 'access_size': S} where N is the sanitizer-authoritative
    initialized-prefix length (first poisoned byte offset) and S the read size.
    """
    lowered = sanitizer_text.lower()
    if "memorysanitizer" not in lowered and "use-of-uninitialized-value" not in lowered:
        return None
    match = _MSAN_UNINIT_RE.search(sanitizer_text)
    if not match:
        return None
    return {"init_prefix_len": int(match.group(1)), "access_size": int(match.group(2))}


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
        if assertion.get("kind") == "observed":
            target_prefix = f"${assertion['at']}."
            operands = check[1:]
            if not all(
                isinstance(operand, str) and operand.startswith(target_prefix)
                for operand in operands
            ):
                raise ValueError(
                    f"observed assertion {assertion_id} must use only fields from "
                    f"its at event {assertion['at']!r}; use a transition assertion "
                    "for cross-event relations"
                )
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
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    actual_content_hash = assertion_content_hash(spec)
    if spec.get("content_hash") != actual_content_hash:
        spec["content_hash"] = actual_content_hash
        spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    spec_bytes = spec_path.read_bytes()
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
    """Require the invariant graph to already satisfy the GT contract.

    Stage 04A must write the new graph shape directly. There is no legacy
    root-cause side channel: root_cause_criterion is only a pointer to a real
    root_cause node, and nodes/edges both carry source operands plus relation.
    """
    selected: set[str] = set()
    errors: list[str] = []
    skipped_unverified: list[str] = []
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
    sections = [("node", item) for item in nodes] + [("edge", item) for item in edges]
    section_by_id: dict[str, str] = {}
    if "refuted" in verified_invariants:
        errors.append("verified_invariants.json must not contain a refuted section")
    for section, item in sections:
        invariant_id = str(item.get("invariant_id") or "").strip()
        verified_flag = item.get("verified", True)
        if verified_flag is not True:
            if item.get("verification_status") != "omitted_not_runtime_verified":
                errors.append(
                    f"{section} invariant {item.get('invariant_id', '<missing>')} "
                    "is not verified"
                )
                continue
            if invariant_id:
                skipped_unverified.append(invariant_id)
            continue
        if not invariant_id:
            errors.append("selected invariant is missing invariant_id")
        elif invariant_id in selected:
            errors.append(f"duplicate invariant_id: {invariant_id}")
        else:
            selected.add(invariant_id)
            section_by_id[invariant_id] = section
        if not item.get("operands"):
            errors.append(f"{section} invariant {invariant_id or '<missing>'} missing operands")
        if not isinstance(item.get("relation"), dict):
            errors.append(f"{section} invariant {invariant_id or '<missing>'} missing relation")

    node_ids = {
        str(item.get("invariant_id"))
        for item in nodes
        if item.get("invariant_id") and str(item.get("invariant_id")) in selected
    }
    for edge in edges:
        edge_id = str(edge.get("invariant_id") or "")
        if edge_id not in selected:
            continue
        if edge.get("from_node") not in node_ids or edge.get("to_node") not in node_ids:
            errors.append(f"edge {edge_id} has unresolved from_node/to_node")

    covered: set[str] = set()
    edge_ids = {
        str(item.get("invariant_id"))
        for item in edges
        if item.get("invariant_id") and str(item.get("invariant_id")) in selected
    }
    edge_transition_coverage: dict[str, list[str]] = {
        invariant_id: [] for invariant_id in edge_ids
    }
    for assertion in spec.get("assertions", []):
        assertion_id = assertion.get("id", "<missing>")
        assertion_invariants = {
            str(invariant_id) for invariant_id in assertion.get("invariants", [])
        }
        assertion_edges = sorted(assertion_invariants & edge_ids)
        if assertion.get("kind") == "transition":
            if assertion_edges and len(assertion_edges) != 1:
                errors.append(
                    f"transition assertion {assertion_id} must cover exactly one edge; "
                    f"got {assertion_edges}"
                )
            elif assertion_edges:
                edge_transition_coverage[assertion_edges[0]].append(str(assertion_id))
        elif assertion_edges:
            errors.append(
                f"non-transition assertion {assertion_id} cannot cover edges: "
                + ", ".join(assertion_edges)
            )
        for invariant_id in assertion.get("invariants", []):
            if invariant_id not in selected:
                if assertion.get("kind") == "required":
                    errors.append(
                        f"required assertion {assertion_id} references unselected "
                        f"invariant {invariant_id}"
                    )
            else:
                covered.add(invariant_id)
    source_identity_nodes = {
        str(node.get("invariant_id"))
        for node in nodes
        if node.get("role") == "source"
        and isinstance(node.get("relation"), dict)
        and node["relation"].get("op") == "same_object"
    }
    for invariant_id in sorted(selected - covered - source_identity_nodes):
        errors.append(f"invariant {invariant_id} has no assertion")

    if not isinstance(criterion, dict) or not str(criterion.get("invariant_id") or "").strip():
        errors.append(
            "root_cause_criterion is null/missing: Stage 04 must point it at the "
            "root_cause node covered by the required assertion"
        )
    else:
        criterion_id = str(criterion.get("invariant_id") or "").strip()
        root_nodes = [
            node for node in nodes
            if str(node.get("invariant_id") or "") == criterion_id
            and node.get("role") == "root_cause"
        ]
        if not root_nodes:
            errors.append(
                f"root_cause_criterion {criterion_id!r} does not point to a "
                "nodes[] entry with role='root_cause'"
            )
        linked = [
            str(assertion.get("id"))
            for assertion in spec.get("assertions", [])
            if assertion.get("kind") == "required"
            and criterion_id in {str(x) for x in assertion.get("invariants", [])}
        ]
        if not linked:
            errors.append(
                f"root_cause_criterion {criterion_id!r} has no required assertion bound to it"
            )
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
        "skipped_unverified": skipped_unverified,
        "errors": errors,
    }


# `$event.field` suffixes that resolve to a compile-time literal (NULL/0/etc.)
# and therefore never need a field_bindings entry -- mirrors the downstream
# probe builder's literal set so this gate stays consistent with it.
_LITERAL_FIELD_SUFFIXES = {"null_literal", "zero_literal", "true_literal", "false_literal"}


def validate_binding_coverage(
    spec: dict[str, Any],
    field_bindings: dict[str, Any] | None,
    event_locations: dict[str, Any] | None,
) -> dict[str, Any]:
    """Check that the two Stage-04 side maps actually cover what the verified
    assertions reference.

    event_locations (ERROR): every event id used as an assertion `at`/`from`/`protects`
    must resolve to a real (function, file). Without it the probe question
    splices the synthetic event id into its text -- unanswerable by
    construction (a subject greps for `enqueue_deferred`, finds nothing). This
    is a hard error: empirically every current sample already covers 100% of
    its events, so requiring it only guards against regression.

    field_bindings (WARNING): every `$event.field` operand that is not a
    compile-time literal should map to its real source expression (or, for a
    required structural obligation absent from the vulnerable program, the exact
    expression introduced by the fix). Left as a warning, not an error, because the downstream builder
    degrades gracefully for a few operand shapes (an operand whose field is the
    root variable itself, a runtime-captured value with no better name than its
    line) -- so an unbound operand is a quality gap to review, not a structural
    break.
    """
    field_bindings = field_bindings or {}
    event_locations = event_locations or {}
    errors: list[str] = []
    warnings: list[str] = []

    referenced_events: set[str] = set()
    referenced_operands: set[tuple[str, str | None]] = set()
    for assertion in spec.get("assertions", []):
        if not isinstance(assertion, dict):
            continue
        for key in ("at", "from", "protects"):
            ev = assertion.get(key)
            if ev:
                referenced_events.add(str(ev))
        check = assertion.get("check")
        if isinstance(check, list) and len(check) == 3:
            for side in (check[1], check[2]):
                if isinstance(side, str) and side.startswith("$"):
                    referenced_operands.add(
                        (side[1:], str(assertion.get("at") or "") or None)
                    )

    for event in sorted(referenced_events):
        loc = event_locations.get(event)
        if not isinstance(loc, dict) or not loc.get("function") or not loc.get("file"):
            errors.append(
                f"event_locations missing (function,file) for event id {event!r} "
                "referenced by an assertion at/from/protects"
            )

    for operand, default_event in sorted(
        referenced_operands, key=lambda item: (item[0], item[1] or "")
    ):
        if operand.rsplit(".", 1)[-1] in _LITERAL_FIELD_SUFFIXES:
            continue
        contextual_operand = (
            f"{default_event}.{operand}"
            if default_event and "." not in operand
            else operand
        )
        if operand not in field_bindings and contextual_operand not in field_bindings:
            warnings.append(
                f"field_bindings has no real source expression for operand "
                f"{contextual_operand!r} "
                "(probe builder will fall back to an anonymized/positional label)"
            )

    return {
        "valid": not errors,
        "event_count": len(referenced_events),
        "operand_count": len(referenced_operands),
        "errors": errors,
        "warnings": warnings,
    }


def _load_map_file(path: Path | None, key: str) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get(key, {}) if isinstance(data, dict) else {}


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
        if line.startswith("CASE"):
            raise ValueError(
                "malformed CASE line; expected "
                "`CASE name=<name> rc=<int> result=<result>`: " + line
            )
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


def _following_event_with_index(
    case: dict[str, Any], point: str, target_index: int
) -> tuple[int, dict[str, Any]] | None:
    for index in range(target_index + 1, len(case["events"])):
        event = case["events"][index]
        if event.get("point") == point:
            return index, event
    return None


def _protected_event_after_target(
    case: dict[str, Any],
    protected_point: str,
    *,
    target_point: str,
    target_index: int,
) -> tuple[int, dict[str, Any]] | None:
    for index in range(target_index + 1, len(case["events"])):
        event = case["events"][index]
        point = event.get("point")
        if point == protected_point:
            return index, event
        if point == target_point:
            break
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
            if assertion["kind"] == "required" and assertion.get("protects"):
                return {
                    "status": "avoided",
                    "satisfied": True,
                    "triggered": False,
                }
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
        if protected == assertion["at"]:
            triggered = True
        else:
            triggered = _protected_event_after_target(
                case,
                protected,
                target_point=assertion["at"],
                target_index=target_index,
            ) is not None
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
    *,
    differential_applicable: bool = True,
) -> dict[str, Any]:
    validate_frozen_spec(spec)
    original = spec.get("original_case", "original")
    results: list[dict[str, Any]] = []
    semantic_failures: list[dict[str, Any]] = []
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
        fixed_original_case = fixed_cases.get(original) or {}
        fixed_crashed = str(fixed_original_case.get("result") or "") == "crash"
        if assertion["kind"] in {"observed", "transition"}:
            verified = vulnerable_status == "satisfied"
        elif assertion.get("protects"):
            genuine_witness_case = None
            fixed_non_original_cases = [
                name for name in matrix["fixed"] if name != original
            ]
            if fixed_status in _FIXED_GUARD_STATUSES:
                genuine_witness_case = next(
                    (
                        name
                        for name, outcome in matrix["fixed"].items()
                        if name != original and outcome.get("status") == "genuine"
                    ),
                    None,
                )
            fixed_guard_accepted = (
                fixed_status in _FIXED_GUARD_STATUSES
                and not fixed_crashed
                and bool(fixed_non_original_cases)
            )
            verified = vulnerable_status == "violated" and (
                fixed_status == "genuine"
                or fixed_guard_accepted
            )
        else:
            verified = vulnerable_status == "refuted" and fixed_status == "satisfied"
        item = {
            "id": assertion["id"],
            "invariants": list(assertion.get("invariants", [])),
            "kind": assertion["kind"],
            "verified": verified,
            "matrix": matrix,
        }
        if verified:
            # A `required` assertion is the only kind whose verification uses the
            # vulnerable/fixed differential; observed/transition are established on
            # the vulnerable side alone. Recording this keeps `all_verified` from
            # reading as "differentially confirmed" when it is not.
            item["verification"] = (
                "differential"
                if assertion["kind"] == "required"
                else "vulnerable_side_only"
            )
        if assertion.get("protects") and fixed_status in _FIXED_GUARD_STATUSES:
            item["genuine_witness_case"] = genuine_witness_case
            item["fixed_guard_status"] = fixed_status
            item["fixed_guard_clean"] = not fixed_crashed
            item["fixed_perturbation_cases"] = fixed_non_original_cases
            if fixed_guard_accepted:
                item["fixed_guard_acceptance"] = (
                    "fixed original skipped the protected event and exited cleanly; "
                    "one fixed perturbation was attempted and recorded"
                )
            elif genuine_witness_case is None:
                item["verification_error"] = (
                    f"fixed original is {fixed_status}; add exactly one perturbation "
                    "case before accepting the guarded fixed-side witness"
                )
        if (
            assertion.get("protects")
            and fixed_status == "violated"
            and not fixed_crashed
        ):
            # The protected operation cannot have run with its safety obligation
            # violated and left the process clean. The probe for
            # assertion["protects"] is therefore firing somewhere the operation
            # does not, most often at the top of the enclosing block and ahead
            # of the guard the fix introduces.
            item["probe_placement_error"] = (
                f"probe for protected event {assertion['protects']!r} fired on the "
                "fixed run while the required predicate was violated, yet that run "
                "exited cleanly. Move the probe to immediately before the protected "
                "operation, after every guard that can skip it; a probe emitted at "
                "block entry observes reaching the block, not performing the operation"
            )
        if assertion["kind"] == "required" and vulnerable_status != "violated":
            vulnerable_original = matrix["vulnerable"].get(original, {})
            semantic_failures.append({
                "id": assertion["id"],
                "reason": "required root predicate is not violated on the vulnerable original witness",
                "vulnerable_status": vulnerable_status,
                "left": vulnerable_original.get("left"),
                "op": vulnerable_original.get("op"),
                "right": vulnerable_original.get("right"),
            })
        results.append(item)
    required_results = [item for item in results if item.get("kind") == "required"]
    propagation_results = [
        item for item in results if item.get("kind") in {"observed", "transition"}
    ]
    required_verified = bool(required_results) and all(
        item["verified"] for item in required_results
    )
    propagation_all_verified = all(
        item["verified"] for item in propagation_results
    )
    all_verified = bool(results) and required_verified and propagation_all_verified

    if not differential_applicable:
        # e.g. an MSAN use-of-uninitialized-value bug: the safety property is
        # per-byte definedness, which cannot be a vulnerable/fixed comparison of
        # two source fields, so no differential is expected. The sanitizer is the
        # authoritative oracle for these; see parse_msan_uninit / check_msan_offset.
        differential_status = "not_applicable"
    elif any(item["verified"] and item["kind"] == "required" for item in results):
        differential_status = "confirmed"
    elif any(item.get("probe_placement_error") for item in results):
        # Distinct from vulnerable_side_only: nothing is known about the fixed
        # side yet because the instrumentation did not observe what it claimed.
        differential_status = "probe_misplaced"
    else:
        differential_status = "vulnerable_side_only"
    output = {
        "schema_version": (
            "assertion-results-v3"
            if spec.get("schema_version") == "assertion-spec-v3"
            else "assertion-results-v2"
        ),
        "sample_id": spec.get("sample_id"),
        "candidate_content_hash": spec.get("content_hash"),
        "original_case": original,
        "required_verified": required_verified,
        "propagation_all_verified": propagation_all_verified,
        "all_verified": all_verified,
        "differential_status": differential_status,
        "assertions": results,
    }
    if semantic_failures:
        output["failure_class"] = "root_not_violated_on_vulnerable_witness"
        output["stage04b_failure"] = {
            "classification": "semantic_root_failure",
            "message": (
                "At least one required root predicate was already satisfied or "
                "not exercised on the vulnerable original witness. Stage 04A "
                "must choose a safety obligation that the vulnerable crashing "
                "run violates before the protected operation."
            ),
            "evidence": semantic_failures,
        }
    return output


def check_msan_offset(results: dict[str, Any], msan: dict[str, int]) -> dict[str, Any]:
    """Cross-check the frozen assertions against MSAN's authoritative uninit boundary.

    The sink comparison must witness the read size S as one operand and the
    initialized-prefix length N as the other. A GT that records N off by one --
    e.g. counting an unwritten NUL terminator as initialized (`len + 1` instead of
    `len`) -- is refuted here at generation time instead of shipping a wrong length.
    """
    access_size = msan["access_size"]
    init_prefix_len = msan["init_prefix_len"]
    original = str(results.get("original_case") or "original")
    sink_init_operands: dict[str, int] = {}
    for item in results.get("assertions", []):
        outcome = item.get("matrix", {}).get("vulnerable", {}).get(original, {})
        left, right = outcome.get("left"), outcome.get("right")
        if not (isinstance(left, int) and isinstance(right, int)):
            continue
        operands = {left, right}
        if access_size in operands and len(operands) > 1:
            sink_init_operands[str(item["id"])] = next(iter(operands - {access_size}))
    matched = any(value == init_prefix_len for value in sink_init_operands.values())
    check: dict[str, Any] = {
        "applicable": True,
        "init_prefix_len": init_prefix_len,
        "access_size": access_size,
        "sink_assertion_init_operands": sink_init_operands,
        "matched": matched if sink_init_operands else None,
    }
    if not sink_init_operands:
        check["warning"] = (
            f"no assertion witnesses the MSAN read size {access_size}; the "
            "initialized-prefix length was not cross-checked against the sanitizer"
        )
    elif not matched:
        check["error"] = (
            f"MSAN reports the initialized prefix as {init_prefix_len} bytes of a "
            f"{access_size}-byte read (first uninitialized byte at offset {init_prefix_len}), "
            f"but the sink assertion's initialized-length operand is "
            f"{sorted(set(sink_init_operands.values()))}. Bind it to the sanitizer offset "
            "(a common off-by-one is counting an unwritten NUL terminator as "
            "initialized: use `len`, not `len + 1`)."
        )
    return check


def build_verified_assertions(
    spec: dict[str, Any], results: dict[str, Any]
) -> dict[str, Any]:
    verified = {item["id"] for item in results.get("assertions", []) if item["verified"]}
    return {
        "sample_id": spec.get("sample_id"),
        "content_hash": spec.get("content_hash"),
        "assertions": [item for item in spec.get("assertions", []) if item["id"] in verified],
    }


def _claim_location(event_id: str, event_locations: dict[str, Any]) -> dict[str, Any]:
    location = event_locations.get(event_id)
    if not isinstance(location, dict):
        raise ValueError(f"event_locations missing location for {event_id!r}")
    file = str(location.get("file") or "").strip()
    function = str(location.get("function") or "").strip()
    line = location.get("line")
    if not file or not function or not isinstance(line, int) or line < 1:
        raise ValueError(f"event_locations has invalid location for {event_id!r}")
    return {"file": file, "function": function, "line": line}


def _claim_operand(
    value: Any,
    field_bindings: dict[str, Any],
    default_event: str | None,
) -> str:
    if isinstance(value, str) and value.startswith("$"):
        suffix = value[1:].rsplit(".", 1)[-1]
        literal_values = {
            "zero_literal": "0",
            "null_literal": "nullptr",
            "true_literal": "true",
            "false_literal": "false",
        }
        if suffix in literal_values:
            return literal_values[suffix]
    expression = _resolve_operand_expr(value, field_bindings, default_event).strip()
    if not expression:
        raise ValueError(f"empty expression for assertion operand {value!r}")
    return expression


def _admission_from_ground_truth(ground_truth: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(ground_truth, dict):
        return []
    candidates: list[dict[str, Any]] = []
    reachability = ground_truth.get("reachability_checkpoints")
    if isinstance(reachability, dict):
        parser = reachability.get("parser_admitted")
        if isinstance(parser, dict):
            admitted = parser.get("admitted_location")
            if isinstance(admitted, dict):
                candidates.append(admitted)
            candidates.append(parser)
    for key in ("source", "tainted_value_origin"):
        item = ground_truth.get(key)
        if isinstance(item, dict):
            candidates.append(item)
    admission = []
    seen: set[tuple[str, str, int]] = set()
    for item in candidates:
        file = str(item.get("file") or "").strip()
        function = str(item.get("function") or "").strip()
        line = item.get("line")
        if not file or not function or not isinstance(line, int) or line < 1:
            continue
        key = (file, function, line)
        if key in seen:
            continue
        seen.add(key)
        admission.append(
            {
                "id": f"admission_{len(admission) + 1:02d}",
                "at": {"file": file, "function": function, "line": line},
            }
        )
        if len(admission) == 3:
            break
    return admission


def build_assertion_reward_spec(
    verified_assertions: dict[str, Any],
    field_bindings: dict[str, Any] | None,
    event_locations: dict[str, Any] | None,
    *,
    ground_truth: dict[str, Any] | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Project verified GT assertions into the reward framework claim schema."""
    field_bindings = field_bindings or {}
    event_locations = event_locations or {}
    assertions = [
        item
        for item in verified_assertions.get("assertions", [])
        if isinstance(item, dict)
    ]
    required = [item for item in assertions if item.get("kind") == "required"]
    admission = _admission_from_ground_truth(ground_truth)
    admission_events = list(
        dict.fromkeys(
            str(item.get("at"))
            for item in required
            if str(item.get("at") or "").strip()
        )
    )
    if not admission_events:
        admission_events = list(
            dict.fromkeys(
                str(item.get("at"))
                for item in assertions
                if str(item.get("at") or "").strip()
            )
        )
    if not admission:
        admission = [
            {"id": f"admission_{index:02d}", "at": _claim_location(event_id, event_locations)}
            for index, event_id in enumerate(admission_events[:3], 1)
        ]
    claims = []
    for assertion in assertions:
        kind = str(assertion.get("kind") or "")
        if kind not in {"required", "observed", "transition"}:
            continue
        check = assertion.get("check")
        if not isinstance(check, list) or len(check) != 3:
            raise ValueError(f"assertion {assertion.get('id')} has invalid check")
        at_event = str(assertion.get("at") or "")
        claim = {
            "id": str(assertion.get("id") or ""),
            "kind": kind,
            "at": _claim_location(at_event, event_locations),
            "from": None,
            "check": {
                "op": str(check[0]),
                "left": _claim_operand(check[1], field_bindings, at_event),
                "right": _claim_operand(check[2], field_bindings, at_event),
            },
        }
        if kind == "transition":
            from_event = str(assertion.get("from") or "")
            if not from_event:
                raise ValueError(f"transition assertion {assertion.get('id')} is missing from")
            claim["from"] = _claim_location(from_event, event_locations)
        claims.append(claim)
    spec = {
        "protocol": "assertion-reward-v1",
        "admission": admission,
        "claims": claims,
    }
    reward_module = _load_assertion_reward_module()

    parsed = reward_module.AssertionRewardSpec.from_dict(spec)
    if source_root is not None:
        reward_module.validate_spec_sources(parsed, source_root)
    return parsed.to_dict()


def build_verified_invariants(
    candidate: dict[str, Any], results: dict[str, Any]
) -> dict[str, Any]:
    """Keep only the runtime-verified subset of a new-contract invariant graph."""
    verified_invariant_ids: set[str] = set()
    for assertion in results.get("assertions", []):
        if not assertion.get("verified"):
            continue
        verified_invariant_ids.update(
            str(invariant_id) for invariant_id in assertion.get("invariants", [])
        )

    candidate_nodes = [
        item for item in candidate.get("nodes", []) if isinstance(item, dict)
    ]
    candidate_edges = [
        item for item in candidate.get("edges", []) if isinstance(item, dict)
    ]
    selected_edges = [
        item
        for item in candidate_edges
        if str(item.get("invariant_id") or "") in verified_invariant_ids
    ]
    edge_endpoint_ids = {
        str(item.get(key) or "")
        for item in selected_edges
        for key in ("from_node", "to_node")
        if item.get(key)
    }

    def keep_node(node: dict[str, Any]) -> bool:
        invariant_id = str(node.get("invariant_id") or "")
        relation = node.get("relation")
        is_source_identity = (
            node.get("role") == "source"
            and isinstance(relation, dict)
            and relation.get("op") == "same_object"
        )
        return (
            invariant_id in verified_invariant_ids
            or invariant_id in edge_endpoint_ids
            or is_source_identity
        )

    output = dict(candidate)
    output.pop("schema_version", None)
    criterion = candidate.get("root_cause_criterion")
    if isinstance(criterion, dict):
        criterion_id = str(criterion.get("invariant_id") or "")
        output["root_cause_criterion"] = {"invariant_id": criterion_id}
    output["nodes"] = [
        item
        for item in candidate_nodes
        if keep_node(item)
    ]
    output["edges"] = selected_edges
    return output


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
        if (
            item["matrix"].get("fixed", {}).get(original, {}).get("status")
            in _FIXED_GUARD_STATUSES
            and item["matrix"].get("vulnerable", {}).get(original, {}).get("status")
            == "violated"
        )
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
    attempted_cases = [case for case in cases if case.get("name")]
    single_attempt_recorded = (not needed_ids) or bool(attempted_cases)
    return {
        "schema_version": "perturbation-results-v1",
        "sample_id": spec.get("sample_id"),
        "needed": bool(needed_ids),
        "reason": (
            "fixed original skipped protected operation for: " + ", ".join(needed_ids)
            if needed_ids else
            "fixed original did not leave a guarded/avoided required assertion"
        ),
        "cases": cases,
        "genuine_witness_cases": witnesses,
        "all_needed_witnessed": all_needed_witnessed,
        "single_perturbation_attempt_recorded": single_attempt_recorded,
        "accepted_after_single_attempt": bool(needed_ids)
        and single_attempt_recorded
        and not all_needed_witnessed,
    }


def _format_operand(value: Any) -> str:
    if isinstance(value, str) and value.startswith("$"):
        return value[1:]
    return json.dumps(value, ensure_ascii=False)


def format_check(check: list[Any]) -> str:
    op_name, left, right = check
    return f"{_format_operand(left)} {_SYMBOLS[op_name]} {_format_operand(right)}"


_INEQUALITY_OPS = {"lt", "le", "gt", "ge"}


def _resolve_operand_expr(
    value: Any,
    field_bindings: dict[str, Any],
    default_event: str | None = None,
) -> str:
    """Resolve an assertion operand to its real source/patch expression."""
    if not (isinstance(value, str) and value.startswith("$")):
        return json.dumps(value, ensure_ascii=False)
    stripped = value[1:]
    contextual = (
        f"{default_event}.{stripped}"
        if default_event and "." not in stripped
        else stripped
    )
    if contextual in field_bindings:
        return binding_expr(field_bindings, contextual)
    return binding_expr(field_bindings, stripped, contextual)


def _norm_expr(text: str) -> str:
    return "".join(str(text).split())


def annotate_scored_invariants(
    verified_invariants: dict[str, Any],
    verified_assertions: dict[str, Any],
    field_bindings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify verified invariants into reasoning scoring keys -- an EVAL-TIME
    selection helper, NOT a GT stage.

    The GT (verified_invariants.json) is left untouched: which invariants are worth
    scoring is a selection policy, so it is decided at evaluation time (call this on
    an in-memory copy) and never persisted into GT -- changing the policy must not
    invalidate or regenerate ground truth. Every input it needs already lives in the
    frozen GT artifacts (verified_invariants + verified_assertions + field_bindings),
    so no information is lost by not marking the files. Returns the same dict with
    per-item `scored`/`scored_role` added.

      edge -> `scored` / `scored_role`:
        a DATA edge whose `eq` operands resolve (via field_bindings) to the SAME
        source expression is pure connectivity -- e.g. `oid == oid`, the buffer read
        is the buffer used at the sink -- and carries no reasoning content, so
        scored=false, scored_role="connectivity". Everything else is a real step:
        scored=true, scored_role="reasoning". Crucially this is type-gated: an
        `order` edge (free_before_use / double_free) or a `control` edge is NEVER
        demoted even when its check is an eq on one object, because the happens-before
        / missing-guard IS the reasoning for temporal and control bugs. An `eq`
        between two DIFFERENT expressions (aliasing, e.g. `cur->name == id->name`)
        also stays reasoning.
      node -> `scored` / `scored_role` / `relation`:
        a node whose verifying assertion is an OBSERVED inequality carries the
        quantitative mechanism -- for an uninitialized-read bug, read length >
        initialized length -- so scored=true, scored_role="mechanism". This is the
        discriminative fact a subject must recover, distinct from the connectivity
        edges and from the narrative root cause.
    """
    field_bindings = field_bindings or {}
    by_id: dict[str, dict[str, Any]] = {}
    by_invariant: dict[str, dict[str, Any]] = {}
    for assertion in verified_assertions.get("assertions", []):
        if not isinstance(assertion, dict):
            continue
        if assertion.get("id"):
            by_id[str(assertion["id"])] = assertion
        for invariant_id in assertion.get("invariants", []):
            by_invariant.setdefault(str(invariant_id), assertion)

    def _assertion_for(item: dict[str, Any]) -> dict[str, Any] | None:
        verified_by = str(item.get("verified_by") or "")
        if verified_by in by_id:
            return by_id[verified_by]
        return by_invariant.get(str(item.get("invariant_id") or ""))

    for edge in verified_invariants.get("edges", []):
        if not isinstance(edge, dict):
            continue
        assertion = _assertion_for(edge)
        check = assertion.get("check") if assertion else None
        if not isinstance(check, list) or len(check) != 3:
            continue
        op, left, right = check
        # Only a DATA edge can be pure connectivity. An order/control edge encodes
        # happens-before or a missing guard -- reasoning -- even when its check is an
        # eq on a single object (e.g. free-before-free of the same pointer).
        edge_type = str(edge.get("type") or "data").lower()
        default_event = str(assertion.get("at") or "") or None
        left_expr = _resolve_operand_expr(left, field_bindings, default_event)
        right_expr = _resolve_operand_expr(right, field_bindings, default_event)
        is_identity_flow = (
            op == "eq"
            and edge_type == "data"
            and _norm_expr(left_expr) == _norm_expr(right_expr)
        )
        if is_identity_flow:
            edge["scored"] = False
            edge["scored_role"] = "connectivity"
            edge["scored_reason"] = (
                f"eq identity flow on a data edge: both operands resolve to "
                f"`{left_expr}` (connectivity, not a reasoning step)"
            )
        else:
            edge["scored"] = True
            edge["scored_role"] = "reasoning"

    for node in verified_invariants.get("nodes", []):
        if not isinstance(node, dict):
            continue
        assertion = _assertion_for(node)
        if not assertion or assertion.get("kind") != "observed":
            continue
        check = assertion.get("check")
        if not isinstance(check, list) or len(check) != 3 or check[0] not in _INEQUALITY_OPS:
            continue
        op, left, right = check
        node["scored"] = True
        node["scored_role"] = "mechanism"
        default_event = str(assertion.get("at") or "") or None
        node["relation"] = (
            f"{_resolve_operand_expr(left, field_bindings, default_event)} {_SYMBOLS[op]} "
            f"{_resolve_operand_expr(right, field_bindings, default_event)}"
        )
    return verified_invariants


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--vulnerable-trace", type=Path)
    parser.add_argument("--fixed-trace", type=Path)
    parser.add_argument("--results-out", type=Path)
    parser.add_argument("--verified-invariants", type=Path)
    parser.add_argument("--verified-assertions-out", type=Path)
    parser.add_argument("--perturbation-results-out", type=Path)
    parser.add_argument("--assertion-reward-spec-out", type=Path)
    parser.add_argument("--ground-truth", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--check-bindings-only", action="store_true")
    parser.add_argument("--field-bindings", type=Path, help="field_bindings.json for the binding-coverage gate")
    parser.add_argument("--event-locations", type=Path, help="event_locations.json for the binding-coverage gate")
    parser.add_argument(
        "--sanitizer-trace",
        type=Path,
        help="sanitizer_trace.txt; when it is an MSAN uninitialized-value trace, sets "
        "differential_status=not_applicable and runs the offset-checksum gate",
    )
    parser.add_argument("--freeze-only", action="store_true")
    parser.add_argument("--freeze-marker", type=Path)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    if args.freeze_only:
        if not args.freeze_marker:
            parser.error("--freeze-only requires --freeze-marker")
        print(json.dumps(freeze_spec(args.spec, args.freeze_marker), indent=2))
        return 0
    validate_frozen_spec(spec)
    if args.check_bindings_only:
        if not args.verified_invariants:
            parser.error("--check-bindings-only requires --verified-invariants")
        binding = validate_invariant_bindings(
            json.loads(args.verified_invariants.read_text(encoding="utf-8")), spec
        )
        report: dict[str, Any] = {"invariant_binding": binding}
        ok = binding["valid"]
        # Binding-coverage gate over the Stage-04 side maps, when provided.
        if args.field_bindings or args.event_locations:
            coverage = validate_binding_coverage(
                spec,
                _load_map_file(args.field_bindings, "bindings"),
                _load_map_file(args.event_locations, "locations"),
            )
            report["binding_coverage"] = coverage
            ok = ok and coverage["valid"]
        print(json.dumps(report, indent=2))
        if not ok:
            raise SystemExit(1)
        return 0
    if not args.vulnerable_trace or not args.fixed_trace or not args.results_out:
        parser.error(
            "validation requires --vulnerable-trace, --fixed-trace, and --results-out"
        )
    msan = None
    if args.sanitizer_trace and args.sanitizer_trace.exists():
        msan = parse_msan_uninit(
            args.sanitizer_trace.read_text(encoding="utf-8", errors="replace")
        )
    results = validate_assertions(
        spec,
        parse_trace_matrix(args.vulnerable_trace.read_text(encoding="utf-8")),
        parse_trace_matrix(args.fixed_trace.read_text(encoding="utf-8")),
        differential_applicable=not bool(msan),
    )
    if msan:
        results["msan_offset_check"] = check_msan_offset(results, msan)
    verified_invariants = None
    projected_verified_invariants = None
    if args.verified_invariants:
        verified_invariants = json.loads(
            args.verified_invariants.read_text(encoding="utf-8")
        )
        projected_verified_invariants = build_verified_invariants(
            verified_invariants, results
        )
        results["invariant_binding"] = validate_invariant_bindings(
            projected_verified_invariants, spec
        )
    args.results_out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    if args.verified_invariants and projected_verified_invariants is not None:
        args.verified_invariants.write_text(
            json.dumps(projected_verified_invariants, indent=2)
            + "\n",
            encoding="utf-8",
        )
    if args.verified_assertions_out:
        verified_assertions = build_verified_assertions(spec, results)
        args.verified_assertions_out.write_text(
            json.dumps(verified_assertions, indent=2) + "\n", encoding="utf-8"
        )
    else:
        verified_assertions = build_verified_assertions(spec, results)
    if args.perturbation_results_out:
        args.perturbation_results_out.write_text(
            json.dumps(build_perturbation_results(spec, results), indent=2) + "\n",
            encoding="utf-8",
        )
    if args.assertion_reward_spec_out:
        if not args.field_bindings or not args.event_locations:
            parser.error(
                "--assertion-reward-spec-out requires --field-bindings and --event-locations"
            )
        field_bindings = _load_map_file(args.field_bindings, "bindings")
        event_locations = _load_map_file(args.event_locations, "locations")
        ground_truth = (
            json.loads(args.ground_truth.read_text(encoding="utf-8"))
            if args.ground_truth and args.ground_truth.exists()
            else None
        )
        args.assertion_reward_spec_out.write_text(
            json.dumps(
                build_assertion_reward_spec(
                    verified_assertions,
                    field_bindings,
                    event_locations,
                    ground_truth=ground_truth,
                    source_root=args.source_root,
                ),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    offset_error = bool(results.get("msan_offset_check", {}).get("error"))
    if results.get("invariant_binding", {}).get("valid") is False:
        raise ValueError("invalid invariant bindings")
    if not results["required_verified"] or offset_error:
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
