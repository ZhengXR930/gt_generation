"""Reject invalid Stage-04 assertion plans before instrumentation starts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .assertions import (
    validate_binding_coverage,
    validate_frozen_spec,
    validate_invariant_bindings,
)
from .evidence import file_sha256
from .package_audit import _verified_invariant_harness_errors


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return value


def _validate_patch_syntax(path: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "apply", "--numstat", "--", str(path.resolve())],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError as exc:
        return [f"cannot validate instrumentation patch {path.name}: {exc}"]
    if completed.returncode == 0 and completed.stdout.strip():
        return []
    detail = (completed.stderr or completed.stdout).strip()
    if not detail:
        detail = "patch contains no file changes"
    return [f"invalid instrumentation patch {path.name}: {detail}"]


def _norm_expr(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _field_expr(
    operand: Any,
    field_bindings: dict[str, Any],
    *,
    default_event: str = "",
) -> str:
    """Resolve a `$event.field` assertion operand to its source expression."""
    if not isinstance(operand, str):
        return str(operand)
    if not operand.startswith("$"):
        return operand
    key = operand[1:]
    candidates = [key]
    if default_event and "." not in key:
        candidates.append(f"{default_event}.{key}")
    for candidate in candidates:
        binding = field_bindings.get(candidate)
        if isinstance(binding, dict):
            expr = binding.get("expr")
            if expr:
                return str(expr)
        elif isinstance(binding, str):
            return binding
    return key


def _binding_value(binding: Any) -> tuple[str, list[str]]:
    if isinstance(binding, dict):
        expr = str(binding.get("expr") or "")
        aliases = binding.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = []
        return expr, [str(item) for item in aliases]
    if isinstance(binding, str):
        return binding, []
    return "", []


def _binding_for_operand(
    operand: str,
    field_bindings: dict[str, Any],
    default_event: str | None,
) -> tuple[str, Any] | None:
    keys = [operand]
    if default_event and "." not in operand:
        keys.append(f"{default_event}.{operand}")
    for key in keys:
        if key in field_bindings:
            return key, field_bindings[key]
    return None


_LITERAL_FIELD_SUFFIXES = {"null_literal", "zero_literal", "true_literal", "false_literal"}

_PROSE_BINDING_PATTERNS = (
    r"\b(recorded|reported|confirmed)\s+by\s+(asan|msan|ubsan|sanitizer)\b",
    r"\bfor\s+(this|the)\s+(witness|crash|execution|run)\b",
    r"\bexecutes?\s+before\s+the\s+later\b",
    r"\bbefore\s+the\s+later\s+(read|write|free|use|call)\b",
    r"\buses?\s+.+\s+as\s+(its|the)\b",
    r"\bis\s+(the|a|an)\s+\w+",
    r"\bthe\s+(same|current|later|protected|dangerous)\b",
)


def _looks_prose_binding_value(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in _PROSE_BINDING_PATTERNS)


def validate_binding_expression_quality(
    spec: dict[str, Any],
    field_bindings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reject prose in bindings used by runtime assertions.

    Bindings are consumed both by instrumentation and by later deterministic
    matching, so assertion-referenced values must be source expressions,
    literals, or explicit instrumentation scalar names. Natural-language witness
    descriptions belong in assertion/node descriptions or top-level notes.
    """
    field_bindings = field_bindings or {}
    errors: list[str] = []
    checked: set[str] = set()
    for assertion in spec.get("assertions", []):
        if not isinstance(assertion, dict):
            continue
        check = assertion.get("check")
        if not isinstance(check, list) or len(check) != 3:
            continue
        default_event = str(assertion.get("at") or "") or None
        for side in (check[1], check[2]):
            if not isinstance(side, str) or not side.startswith("$"):
                continue
            operand = side[1:]
            if operand.rsplit(".", 1)[-1] in _LITERAL_FIELD_SUFFIXES:
                continue
            found = _binding_for_operand(operand, field_bindings, default_event)
            if not found:
                continue
            key, binding = found
            if key in checked:
                continue
            checked.add(key)
            expr, aliases = _binding_value(binding)
            if _looks_prose_binding_value(expr):
                errors.append(
                    f"field_bindings expression for {key!r} is prose, not a "
                    f"source expression or executable scalar: {expr!r}"
                )
            for alias in aliases:
                if _looks_prose_binding_value(alias):
                    errors.append(
                        f"field_bindings alias for {key!r} is prose, not an "
                        f"equivalent source spelling: {alias!r}"
                    )
    return {"valid": not errors, "checked": sorted(checked), "errors": errors}


def _looks_pointer_operand(value: str) -> bool:
    text = _norm_expr(value)
    return any(token in text for token in ("ptr", "pointer", "byteptr", "addr"))


def _looks_owner_base_operand(value: str) -> bool:
    text = _norm_expr(value)
    return any(
        token in text
        for token in ("owner", "base", "blockbuff", "alloc", "buffer", "buf")
    )


def _is_sentinel_expr(value: str) -> bool:
    return _norm_expr(value) in {"null", "nullptr", "0", "false"}


def _looks_cleanup_function(value: str) -> bool:
    text = _norm_expr(value)
    return any(
        token in text
        for token in (
            "free",
            "destroy",
            "deinit",
            "cleanup",
            "release",
            "close",
            "fini",
            "finalize",
        )
    )


def _assignment_lhs(line: str) -> str | None:
    stripped = line.split("//", 1)[0].strip()
    match = re.search(r"(?<![!<>=])=(?!=)", stripped)
    if not match:
        return None
    lhs = stripped[:match.start()].strip()
    if not lhs:
        return None
    lhs = re.sub(r"^[A-Za-z_][A-Za-z0-9_\s\*]*\s+", "", lhs).strip()
    return lhs or None


def _assigns_expr_to_sentinel(line: str, expr: str) -> bool:
    lhs = _assignment_lhs(line)
    if not lhs or _norm_expr(lhs) != _norm_expr(expr):
        return False
    rhs = line.split("=", 1)[1].split(";", 1)[0]
    return _is_sentinel_expr(rhs)


def _line_has_function_signature(line: str, function: str) -> bool:
    if not function:
        return False
    tail = function.split("::")[-1]
    return bool(re.search(r"\b" + re.escape(tail) + r"\s*\(", line))


def _function_line_bounds(lines: list[str], function: str, line_number: int) -> tuple[int, int]:
    start = max(0, line_number - 1)
    for index in range(max(0, line_number - 80), min(len(lines), line_number + 20)):
        if _line_has_function_signature(lines[index], function):
            start = index
            break
    brace_depth = 0
    seen_body = False
    end = min(len(lines), line_number + 80)
    for index in range(start, len(lines)):
        brace_depth += lines[index].count("{") - lines[index].count("}")
        if "{" in lines[index]:
            seen_body = True
        if seen_body and index >= line_number - 1 and brace_depth <= 0:
            end = index + 1
            break
    return start, end


def _future_sentinel_assignment(
    *,
    source_root: Path | None,
    event_locations: dict[str, Any],
    root_node: dict[str, Any],
    event_id: str,
    expr: str,
) -> str | None:
    if source_root is None:
        return None
    location = event_locations.get(event_id)
    if not isinstance(location, dict):
        location = root_node
    file_name = str(location.get("file") or root_node.get("file") or "")
    line_number = int(location.get("line") or root_node.get("line") or 0)
    function = str(location.get("function") or root_node.get("function") or "")
    if not file_name or line_number <= 0:
        return None
    source_path = source_root / file_name
    if not source_path.is_file():
        return None
    try:
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    _, end = _function_line_bounds(lines, function, line_number)
    for index in range(line_number, min(end, line_number + 40, len(lines))):
        if _assigns_expr_to_sentinel(lines[index], expr):
            return f"{file_name}:{index + 1}: {lines[index].strip()}"
    return None


def validate_root_obligation_quality(
    spec: dict[str, Any],
    invariants: dict[str, Any],
    field_bindings: dict[str, Any],
    event_locations: dict[str, Any] | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Reject general high-confidence Stage-04A root-obligation mistakes.

    This is intentionally narrower than the role prompt. It only blocks shapes
    that are structurally invalid across samples; mechanism-specific judgement
    still belongs to the agent and the vulnerable/fixed runtime check.
    """
    errors: list[str] = []
    warnings: list[str] = []
    criterion = invariants.get("root_cause_criterion")
    criterion_id = (
        str(criterion.get("invariant_id") or "").strip()
        if isinstance(criterion, dict)
        else ""
    )
    root_node = None
    for node in invariants.get("nodes", []):
        if isinstance(node, dict) and str(node.get("invariant_id") or "") == criterion_id:
            root_node = node
            break
    if not root_node:
        return {"valid": True, "errors": errors, "warnings": warnings}

    relation = root_node.get("relation")
    if isinstance(relation, dict):
        op = str(relation.get("op") or "")
        left = str(relation.get("left") or "")
        right = str(relation.get("right") or "")
        if op == "same_object":
            errors.append(
                f"root obligation {criterion_id!r} is an identity relation; "
                "root_cause must be the missing safety predicate, not object identity"
            )
        if op in {"eq", "ne"} and left and right and _norm_expr(left) == _norm_expr(right):
            errors.append(
                f"root obligation {criterion_id!r} compares the same expression on "
                "both sides; this cannot distinguish vulnerable from fixed behavior"
            )

    required = [
        assertion for assertion in spec.get("assertions", [])
        if isinstance(assertion, dict)
        and assertion.get("kind") == "required"
        and criterion_id in {str(item) for item in assertion.get("invariants", [])}
    ]
    for assertion in required:
        mechanism_value = assertion.get("mechanism")
        if not isinstance(mechanism_value, str) or not mechanism_value.strip():
            errors.append(
                f"required root assertion {assertion.get('id')!r} is missing "
                "mechanism; set one of bounds, lifetime, initialization, "
                "string_termination, invalid_free, or other so preflight can "
                "apply mechanism-specific checks"
            )
        check = assertion.get("check")
        if not isinstance(check, list) or len(check) != 3:
            continue
        op, left_ref, right_ref = check
        left_expr = _field_expr(left_ref, field_bindings, default_event=str(assertion.get("at") or ""))
        right_expr = _field_expr(right_ref, field_bindings, default_event=str(assertion.get("at") or ""))
        if op in {"eq", "ne"} and _norm_expr(left_expr) == _norm_expr(right_expr):
            errors.append(
                f"required root assertion {assertion.get('id')!r} compares the "
                f"same source expression {left_expr!r} on both sides"
            )
        mechanism = _norm_expr(assertion.get("mechanism") or assertion.get("description") or "")
        lifetime_mechanism = any(
            token in mechanism
            for token in ("lifetime", "useafterfree", "uaf", "dangling", "doublefree")
        )
        pointer_vs_owner_base = (
            op in {"eq", "ne"}
            and (
                (
                    _looks_pointer_operand(left_ref) or _looks_pointer_operand(left_expr)
                )
                and (
                    _looks_owner_base_operand(right_ref)
                    or _looks_owner_base_operand(right_expr)
                )
                or (
                    (
                        _looks_pointer_operand(right_ref)
                        or _looks_pointer_operand(right_expr)
                    )
                    and (
                        _looks_owner_base_operand(left_ref)
                        or _looks_owner_base_operand(left_expr)
                    )
                )
            )
        )
        if lifetime_mechanism and pointer_vs_owner_base:
            errors.append(
                f"required root assertion {assertion.get('id')!r} uses pointer vs "
                "owner/base/buffer equality as a lifetime obligation. Interior "
                "pointers normally differ from their allocation base even when "
                "valid; use an alive/released/ordering predicate instead"
            )
        if op == "eq":
            sentinel_expr = ""
            target_expr = ""
            if _is_sentinel_expr(left_expr):
                sentinel_expr, target_expr = left_expr, right_expr
            elif _is_sentinel_expr(right_expr):
                sentinel_expr, target_expr = right_expr, left_expr
            if sentinel_expr and target_expr:
                event_location = (event_locations or {}).get(str(assertion.get("at") or ""))
                if not isinstance(event_location, dict):
                    event_location = root_node
                event_function = str(
                    event_location.get("function") or root_node.get("function") or ""
                )
                if (
                    lifetime_mechanism
                    and _looks_cleanup_function(event_function)
                    and (
                        _looks_pointer_operand(target_expr)
                        or _looks_owner_base_operand(target_expr)
                    )
                ):
                    errors.append(
                        f"required root assertion {assertion.get('id')!r} checks "
                        f"a cleanup-time sentinel state {target_expr!r} == "
                        f"{sentinel_expr!r} inside {event_function!r}. For "
                        "lifetime bugs, the root obligation must be the pre-use "
                        "alive/released/ordering predicate that protects the sink, "
                        "not the post-cleanup state left by a free/destroy path"
                    )
                assignment = _future_sentinel_assignment(
                    source_root=source_root,
                    event_locations=event_locations or {},
                    root_node=root_node,
                    event_id=str(assertion.get("at") or ""),
                    expr=target_expr,
                )
                if assignment:
                    errors.append(
                        f"required root assertion {assertion.get('id')!r} checks "
                        f"{target_expr!r} == {sentinel_expr!r} before the source "
                        f"assignment that establishes it ({assignment}). Move the "
                        "event after the assignment or choose the actual pre-use "
                        "safety obligation"
                    )
        joined_operands = "".join(
            str(item) for item in (left_ref, right_ref, left_expr, right_expr)
        )
        if any(
            token in _norm_expr(joined_operands)
            for token in ("capacity", "cap", "blockcomplete", "block_end")
        ):
            warnings.append(
                f"required root assertion {assertion.get('id')!r} looks like a "
                "capacity/block-completion fact; verify it is actually false on "
                "the vulnerable crashing witness and not merely an incidental fact"
            )
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def run_preflight(
    spec_path: Path,
    invariants_path: Path,
    field_bindings_path: Path,
    event_locations_path: Path,
    vulnerable_instrumentation_path: Path | None = None,
    fixed_instrumentation_path: Path | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        spec = _load(spec_path)
        validate_frozen_spec(spec)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"invalid assertion spec: {exc}"]}

    try:
        invariants = _load(invariants_path)
        field_bindings_doc = _load(field_bindings_path)
        event_locations_doc = _load(event_locations_path)
        field_bindings = field_bindings_doc.get("bindings", {})
        event_locations = event_locations_doc.get("locations", {})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"invalid assertion side map: {exc}"]}
    for name, document in (
        ("candidate_invariants.json", invariants),
        ("field_bindings.json", field_bindings_doc),
        ("event_locations.json", event_locations_doc),
    ):
        if "schema_version" in document:
            errors.append(f"{name} must not contain artifact-level schema_version")

    binding = validate_invariant_bindings(invariants, spec)
    errors.extend(binding["errors"])
    errors.extend(_verified_invariant_harness_errors(invariants))
    coverage = validate_binding_coverage(spec, field_bindings, event_locations)
    errors.extend(coverage["errors"])
    binding_quality = validate_binding_expression_quality(spec, field_bindings)
    errors.extend(binding_quality["errors"])
    if source_root is None:
        inferred_source_root = invariants_path.parent / "_work" / "src"
        source_root = inferred_source_root if inferred_source_root.is_dir() else None
    root_quality = validate_root_obligation_quality(
        spec,
        invariants,
        field_bindings,
        event_locations,
        source_root,
    )
    errors.extend(root_quality["errors"])
    input_paths = (
        spec_path,
        invariants_path,
        field_bindings_path,
        event_locations_path,
    )
    optional_paths = (
        vulnerable_instrumentation_path,
        fixed_instrumentation_path,
    )
    for path in optional_paths:
        if path is not None and not path.is_file():
            errors.append(f"missing instrumentation plan: {path.name}")
        elif path is not None:
            errors.extend(_validate_patch_syntax(path))
    committed_paths = input_paths + tuple(
        path for path in optional_paths if path is not None and path.is_file()
    )
    return {
        "schema_version": "assertion-preflight-v1",
        "sample_id": spec.get("sample_id"),
        "assertion_content_hash": spec.get("content_hash"),
        "input_hashes": {
            path.name: file_sha256(path)
            for path in committed_paths
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ok": not errors,
        "errors": errors,
        "invariant_binding": binding,
        "binding_coverage": coverage,
        "binding_expression_quality": binding_quality,
        "root_obligation_quality": root_quality,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--candidate-invariants", type=Path, required=True)
    parser.add_argument("--field-bindings", type=Path, required=True)
    parser.add_argument("--event-locations", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--vulnerable-instrumentation", type=Path)
    parser.add_argument("--fixed-instrumentation", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_preflight(
        args.spec,
        args.candidate_invariants,
        args.field_bindings,
        args.event_locations,
        args.vulnerable_instrumentation,
        args.fixed_instrumentation,
        args.source_root,
    )
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
