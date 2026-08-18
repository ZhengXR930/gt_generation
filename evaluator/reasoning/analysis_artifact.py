"""Validation for the joint fine-trace and vulnerability-logic artifact."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evaluator.reasoning.fine_trace import validate_fine_trace


OPS = {"eq", "ne", "lt", "le", "gt", "ge", "same_object"}
EDGE_TYPES = {"data", "control", "order"}
ORDER_RELATIONS = {
    "free_before_use",
    "double_free",
    "use_before_init",
    "use_after_return",
    "use_after_scope",
}
_BISON_VALUE_RE = re.compile(r"\$\$|\$[0-9]+")
_PHP_VARIABLE_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*|0x[0-9A-Fa-f]+|\d+")
_SOURCE_EXPR_RE = re.compile(
    r"(->|::|[A-Za-z_][A-Za-z0-9_]*\s*\(|\[[^\]]*\]|"
    r"[().*&=<>!+\-/%|^~?:,]|^[-+]?(?:0x[0-9A-Fa-f]+|\d+)(?:[uUlLfF]*)$|"
    r"^\".*\"$|^'.*'$)"
)
_SOURCE_OPERATOR_RE = re.compile(r"(->|::|[.\[\]()+\-*/%&|^~<>!=?:,])")
_PROSE_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "because",
    "before",
    "after",
    "between",
    "condition",
    "data",
    "derived",
    "earlier",
    "later",
    "message",
    "object",
    "packet",
    "processing",
    "reuses",
    "state",
    "the",
    "unsafe",
    "value",
}
_KEY_ROLES = {"source", "root_cause", "sink"}
_NON_SOURCE_ANCHOR_BASENAMES = {
    "analysis.json",
    "manifest.json",
    "prompt.txt",
    "readme.md",
    "result.json",
    "runtime_output.txt",
}
_NON_SOURCE_ANCHOR_SUFFIXES = (".json", ".txt", ".md")
_NON_SOURCE_ANCHOR_EXACT = {
    "",
    ".",
    "<global>",
    "checkpoint",
    "workspace",
}
_HARNESS_FUNCTIONS = {
    "LLVMFuzzerTestOneInput",
    "FuzzerTestOneInput",
    "fuzz",
    "main",
}
_HARNESS_PATH_MARKERS = (
    "/fuzz/",
    "/fuzzer/",
    "/fuzzers/",
    "/fuzzing/",
    "/oss-fuzz/",
    "/tests/fuzz",
    "/test/fuzz",
)


def parse_analysis_artifact(response: str) -> dict[str, Any] | None:
    try:
        value = json.loads((response or "").strip())
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def validate_analysis_artifact(response: str) -> str | None:
    artifact = parse_analysis_artifact(response)
    if artifact is None:
        return "the final answer must be one bare JSON object"
    expected = {"sample_id", "fine_trace", "vuln_logic"}
    if set(artifact) != expected:
        return "the top-level object must contain exactly sample_id, fine_trace, and vuln_logic"
    if not isinstance(artifact.get("sample_id"), str) or not artifact["sample_id"].strip():
        return "sample_id must be a non-empty string"

    fine_error = validate_fine_trace(json.dumps(artifact["fine_trace"]))
    if fine_error:
        return f"fine_trace: {fine_error}"
    logic_error = validate_vuln_logic(json.dumps(artifact["vuln_logic"]))
    if logic_error:
        return f"vuln_logic: {logic_error}"
    return None


def validate_analysis_artifact_quality(response: str) -> str | None:
    """Return a quality error for fields that must be source-matchable.

    This is intentionally a lint rather than the structural schema.  It rejects
    obvious natural-language placeholders in operands/carriers/relations while
    accepting common C/C++ source tokens, macro names, literals, and simple
    function-call expressions.
    """
    error = validate_analysis_artifact(response)
    if error:
        return error
    artifact = parse_analysis_artifact(response)
    assert artifact is not None
    trace = artifact["fine_trace"]
    logic = artifact["vuln_logic"]

    for index, step in enumerate(trace, 1):
        role = step.get("role")
        if role in _KEY_ROLES:
            error = _quality_anchor_error(step, f"fine_trace[{index}]", role=role)
            if error:
                return error

    for label in ("source", "root_cause", "sink"):
        error = _quality_location_error(logic[label], label)
        if error:
            return error
        relation = logic[label].get("relation")
        if relation is not None:
            error = _quality_relation_error(relation, f"{label}.relation")
            if error:
                return error

    for index, edge in enumerate(logic["propagation"], 1):
        prefix = f"propagation[{index}]"
        for endpoint in ("from", "to"):
            error = _quality_location_error(edge[endpoint], f"{prefix}.{endpoint}")
            if error:
                return error
        for via_index, value in enumerate(edge["via"], 1):
            if not _looks_like_source_expression(value):
                return (
                    f"{prefix}.via[{via_index}] must be a concrete source "
                    f"expression, literal, macro, or order keyword; got {value!r}"
                )
        relation = edge.get("relation")
        if relation is not None:
            error = _quality_relation_error(relation, f"{prefix}.relation")
            if error:
                return error
    error = _quality_trace_projection_error(artifact)
    if error:
        return error
    error = _quality_trace_logic_consistency_error(artifact)
    if error:
        return error
    return None


def _quality_location_error(value: dict[str, Any], label: str) -> str | None:
    error = _quality_anchor_error(value, label, role=_role_from_label(label))
    if error:
        return error
    for index, operand in enumerate(value.get("operands") or [], 1):
        if not _looks_like_source_expression(operand):
            return (
                f"{label}.operands[{index}] must be a concrete source expression, "
                f"literal, or macro from the cited source line; got {operand!r}"
            )
    return None


def _quality_relation_error(value: dict[str, Any], label: str) -> str | None:
    for side in ("left", "right"):
        operand = value.get(side)
        if not _looks_like_source_expression(operand):
            return (
                f"{label}.{side} must be a concrete source expression, literal, "
                f"or macro, not prose; got {operand!r}"
            )
    if label in {"root_cause.relation", "sink.relation"}:
        left = re.sub(r"\s+", "", str(value.get("left") or ""))
        right = re.sub(r"\s+", "", str(value.get("right") or ""))
        if value.get("op") in {"eq", "same_object"} and left == right:
            return (
                f"{label} must describe the violated safety condition, not a "
                f"tautological identity such as {value.get('left')!r} == "
                f"{value.get('right')!r}"
            )
    return None


def _quality_anchor_error(value: dict[str, Any], label: str, *, role: Any = None) -> str | None:
    file_value = str(value.get("file") or "").strip().replace("\\", "/")
    basename = file_value.rsplit("/", 1)[-1].lower()
    normalized = file_value.strip().lower()
    if (
        normalized in _NON_SOURCE_ANCHOR_EXACT
        or basename in _NON_SOURCE_ANCHOR_BASENAMES
        or basename.endswith(_NON_SOURCE_ANCHOR_SUFFIXES)
    ):
        return (
            f"{label}.file must cite vulnerable project source, not an "
            f"analysis/checkpoint artifact or workspace placeholder; got {file_value!r}"
        )
    function = str(value.get("function") or "").strip()
    if role in _KEY_ROLES:
        lowered = "/" + normalized.lstrip("/")
        if function in _HARNESS_FUNCTIONS or any(marker in lowered for marker in _HARNESS_PATH_MARKERS):
            return (
                f"{label} is marked as {role} but points to harness/test/fuzz setup; "
                "use the vulnerable project source location for scored logic"
            )
    return None


def _role_from_label(label: str) -> str | None:
    if label in _KEY_ROLES:
        return label
    if label.endswith(".from") or label.endswith(".to"):
        return None
    return None


def _quality_trace_projection_error(artifact: dict[str, Any]) -> str | None:
    trace = artifact.get("fine_trace") if isinstance(artifact, dict) else None
    logic = artifact.get("vuln_logic") if isinstance(artifact, dict) else None
    if not isinstance(trace, list) or not isinstance(logic, dict):
        return None
    trace_steps = [step for step in trace if isinstance(step, dict)]
    for role in ("source", "root_cause", "sink"):
        point = logic.get(role)
        if not isinstance(point, dict):
            continue
        role_steps = [
            step
            for step in trace
            if isinstance(step, dict) and step.get("role") == role
        ]
        if not role_steps:
            return (
                f"vuln_logic.{role} must be projected from fine_trace: mark one "
                f"fine_trace step with role={role!r} and use that step as the anchor"
            )
        if len(role_steps) != 1:
            return (
                f"fine_trace must contain exactly one step marked role={role!r}; "
                f"found {len(role_steps)}"
            )
        if not _same_anchor(role_steps[0], point):
            return (
                f"vuln_logic.{role} must use the same file, function, and line as "
                f"a fine_trace step marked role={role!r}; do not invent a separate "
                "logic anchor outside the role-marked trace"
            )
    propagation = logic.get("propagation")
    if isinstance(propagation, list):
        for index, edge in enumerate(propagation, 1):
            if not isinstance(edge, dict):
                continue
            for endpoint in ("from", "to"):
                point = edge.get(endpoint)
                if not isinstance(point, dict):
                    continue
                if not any(_same_anchor(step, point) for step in trace_steps):
                    return (
                        f"vuln_logic.propagation[{index}].{endpoint} must use the "
                        "same file, function, and line as an existing fine_trace "
                        "step; add or update an intermediate fine_trace step, then "
                        "project the propagation endpoint from that trace step"
                    )
    return None


def _quality_trace_logic_consistency_error(artifact: dict[str, Any]) -> str | None:
    trace = artifact.get("fine_trace") if isinstance(artifact, dict) else None
    logic = artifact.get("vuln_logic") if isinstance(artifact, dict) else None
    if not isinstance(trace, list) or not isinstance(logic, dict):
        return None
    for role in ("source", "root_cause", "sink"):
        point = logic.get(role)
        if not isinstance(point, dict):
            continue
        role_steps = [
            step
            for step in trace
            if isinstance(step, dict) and step.get("role") == role
        ]
        if len(role_steps) != 1:
            continue
        step = role_steps[0]
        context = _step_context(step)
        terms = _logic_terms(point, include_relation=role != "source")
        if terms and not any(_expr_overlaps_context(term, context) for term in terms):
            return (
                f"vuln_logic.{role} operands/relation must be grounded in the "
                f"same fine_trace step marked role={role!r}; none of {terms!r} "
                "appears in that step's var/code"
            )
    return None


def _step_context(step: dict[str, Any]) -> str:
    return " ".join(str(step.get(field) or "") for field in ("var", "code"))


def _logic_terms(point: dict[str, Any], *, include_relation: bool) -> list[str]:
    terms = [
        item
        for item in (point.get("operands") or [])
        if isinstance(item, str) and item.strip()
    ]
    relation = point.get("relation")
    if include_relation and isinstance(relation, dict):
        for field in ("left", "right"):
            value = relation.get(field)
            if isinstance(value, str) and value.strip():
                terms.append(value)
    return terms


def _expr_overlaps_context(expr: Any, context: str) -> bool:
    expr_text = _expr_key(expr)
    context_text = _expr_key(context)
    if not expr_text or not context_text:
        return False
    if expr_text in context_text or context_text in expr_text:
        return True
    expr_tokens = _expr_tokens(expr)
    context_tokens = _expr_tokens(context)
    return bool(expr_tokens and context_tokens and (expr_tokens & context_tokens))


def _expr_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("->", ".").lower()


def _expr_tokens(value: Any) -> set[str]:
    ignored = {
        "and",
        "or",
        "the",
        "for",
        "from",
        "into",
        "with",
        "true",
        "false",
        "null",
        "nullptr",
        "return",
        "static",
        "const",
        "unsigned",
        "signed",
        "int",
        "char",
        "void",
        "auto",
        "struct",
        "class",
    }
    return {
        token.lower()
        for token in _TOKEN_RE.findall(str(value or ""))
        if len(token) > 1 and token.lower() not in ignored
    }


def _same_anchor(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        _same_file(left.get("file"), right.get("file"))
        and _norm_function(left.get("function")) == _norm_function(right.get("function"))
        and left.get("line") == right.get("line")
        and isinstance(left.get("line"), int)
    )


def _same_file(left: Any, right: Any) -> bool:
    a = _norm_path(left)
    b = _norm_path(right)
    return bool(a and b and (a == b or a.endswith("/" + b) or b.endswith("/" + a)))


def _norm_path(value: Any) -> str:
    path = str(value or "").replace("\\", "/").strip()
    for prefix in ("repo-vul/src-vul/", "src-vul/", "./"):
        while path.startswith(prefix):
            path = path[len(prefix):]
    return re.sub(r"/+", "/", path)


def _norm_function(value: Any) -> str:
    text = str(value or "").strip().split("(", 1)[0].strip()
    parts = text.split()
    if parts:
        text = parts[-1]
    return re.sub(r"\s+", "", text)


def _looks_like_source_expression(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or "\n" in text or "\r" in text:
        return False
    if text in ORDER_RELATIONS:
        return True
    if _BISON_VALUE_RE.fullmatch(text):
        return True
    if _PHP_VARIABLE_RE.fullmatch(text):
        return True
    if text in {r"\"", r"\'"}:
        return True
    lowered = text.lower()
    words = [word.lower() for word in _IDENTIFIER_RE.findall(text)]
    has_source_syntax = bool(_SOURCE_EXPR_RE.search(text))
    is_single_identifier = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text))
    is_field_path = bool(
        re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:(?:->|\.)[A-Za-z_][A-Za-z0-9_]*)+",
            text,
        )
    )
    is_macro = bool(re.fullmatch(r"[A-Z_][A-Z0-9_]*", text))
    is_literal = bool(
        re.fullmatch(r"[-+]?(?:0x[0-9A-Fa-f]+|\d+)(?:[uUlLfF]*)", text)
        or re.fullmatch(r'\".*\"', text)
        or re.fullmatch(r"'.*'", text)
        or text in {"NULL", "nullptr", "true", "false"}
    )
    if is_single_identifier or is_field_path or is_macro or is_literal:
        return True
    if has_source_syntax and _SOURCE_OPERATOR_RE.search(text):
        return True
    if len(words) >= 3 and not has_source_syntax:
        return False
    if len(words) >= 3 and any(word in _PROSE_WORDS for word in words) and not has_source_syntax:
        return False
    if lowered.startswith(("the ", "a ", "an ")) or lowered.endswith(
        (" state", " condition", " value", " object", " data")
    ):
        return False
    return has_source_syntax


def write_analysis_artifact(path: Path, response: str) -> dict[str, Any]:
    error = validate_analysis_artifact(response)
    if error is not None:
        raise ValueError(error)
    artifact = parse_analysis_artifact(response)
    assert artifact is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return artifact


def parse_vuln_logic(response: str) -> dict[str, Any] | None:
    try:
        value = json.loads((response or "").strip())
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _relation_error(value: Any, label: str) -> str | None:
    if not isinstance(value, dict):
        return f"{label} must be an object"
    if set(value) != {"op", "left", "right"}:
        return f"{label} must contain exactly op, left, and right"
    if value.get("op") not in OPS:
        return f"{label}.op must be one of {', '.join(sorted(OPS))}"
    for side in ("left", "right"):
        if not isinstance(value.get(side), str) or not value[side].strip():
            return f"{label}.{side} must be a non-empty verbatim source expression"
    return None


def _location_error(value: Any, label: str, *, require_relation: bool) -> str | None:
    if not isinstance(value, dict):
        return f"{label} must be an object"
    for field in ("file", "function"):
        if not str(value.get(field) or "").strip():
            return f"{label}.{field} must be a non-empty string"
    if not isinstance(value.get("line"), int):
        return f"{label}.line must be an integer"
    operands = value.get("operands")
    if (
        not isinstance(operands, list)
        or not operands
        or not all(isinstance(item, str) and item.strip() for item in operands)
    ):
        return f"{label}.operands must be a non-empty string array"
    if require_relation:
        error = _relation_error(value.get("relation"), f"{label}.relation")
        if error:
            return error
    elif "relation" in value:
        return f"{label}.relation is allowed only for root_cause and sink"
    if "op" in value:
        return f"{label}.op is not supported; use relation.op when a relation is required"
    return None


def validate_vuln_logic(response: str) -> str | None:
    logic = parse_vuln_logic(response)
    if logic is None:
        return "vuln_logic must be one bare JSON object"
    expected = {"source", "root_cause", "sink", "propagation"}
    allowed = expected | {"issue_alignment"}
    keys = set(logic)
    if not expected <= keys or not keys <= allowed:
        return (
            "vuln_logic must contain source, root_cause, sink, propagation, "
            "and optional issue_alignment"
        )
    error = _location_error(logic.get("source"), "source", require_relation=False)
    if error:
        return error
    for label in ("root_cause", "sink"):
        error = _location_error(logic.get(label), label, require_relation=True)
        if error:
            return error
    propagation = logic.get("propagation")
    if not isinstance(propagation, list):
        return "propagation must be an array"
    for index, edge in enumerate(propagation, 1):
        prefix = f"propagation[{index}]"
        if not isinstance(edge, dict):
            return f"{prefix} must be an object"
        if set(edge) not in (
            {"from", "to", "type", "via"},
            {"from", "to", "type", "via", "relation"},
        ):
            return f"{prefix} must contain from, to, type, via, and optional relation"
        if edge.get("type") not in EDGE_TYPES:
            return f"{prefix}.type must be data, control, or order"
        for endpoint in ("from", "to"):
            error = _location_error(edge.get(endpoint), f"{prefix}.{endpoint}", require_relation=False)
            if error:
                return error
        via = edge.get("via")
        if (
            not isinstance(via, list)
            or not via
            or not all(isinstance(item, str) and item.strip() for item in via)
        ):
            return f"{prefix}.via must be a non-empty string array"
        if "relation" in edge:
            error = _relation_error(edge.get("relation"), f"{prefix}.relation")
            if error:
                return error
    if "issue_alignment" in logic:
        alignment = logic.get("issue_alignment")
        expected_alignment = {"admission", "source", "root_cause", "propagation", "sink"}
        if not isinstance(alignment, dict):
            return "issue_alignment must be an object"
        if set(alignment) != expected_alignment:
            return (
                "issue_alignment must contain exactly admission, source, "
                "root_cause, propagation, and sink"
            )
        for field in sorted(expected_alignment):
            if not isinstance(alignment.get(field), str) or not alignment[field].strip():
                return f"issue_alignment.{field} must be a non-empty string"
    return None


def write_vuln_logic(path: Path, response: str) -> dict[str, Any]:
    error = validate_vuln_logic(response)
    if error is not None:
        raise ValueError(error)
    logic = parse_vuln_logic(response)
    assert logic is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(logic, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return logic
