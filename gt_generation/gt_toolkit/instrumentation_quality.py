"""Static checks for Stage-04 observation patches.

These checks are intentionally conservative. They do not try to prove that an
instrumentation expression is semantically perfect; they only reject common
plan-breaking cases where a required runtime field is hard-coded to the answer
instead of being measured from program state.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any


_LITERAL_FIELD_SUFFIXES = {
    "null_literal",
    "zero_literal",
    "true_literal",
    "false_literal",
}

_STATIC_LITERAL_VALUES = {
    "0",
    "1",
    "true",
    "false",
    "null",
    "nullptr",
}


def _norm(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _binding_map(field_bindings: dict[str, Any]) -> dict[str, Any]:
    bindings = field_bindings.get("bindings")
    if isinstance(bindings, dict):
        return bindings
    return field_bindings


def _binding_expr(key: str, bindings: dict[str, Any]) -> str:
    value = bindings.get(key)
    if isinstance(value, dict):
        return str(value.get("expr") or "")
    if isinstance(value, str):
        return value
    return ""


def _is_literal_field(field: str, binding_expr: str) -> bool:
    if field.rsplit(".", 1)[-1] in _LITERAL_FIELD_SUFFIXES:
        return True
    text = _norm(binding_expr)
    if text in _STATIC_LITERAL_VALUES or re.fullmatch(r"[-+]?\d+", text):
        return True
    original = str(binding_expr or "").strip()
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", original):
        return True
    if original.startswith("sizeof("):
        return True
    return False


def required_runtime_fields(
    spec: dict[str, Any], field_bindings: dict[str, Any]
) -> set[tuple[str, str]]:
    """Return non-literal `$event.field` operands used by required assertions."""
    bindings = _binding_map(field_bindings)
    fields: set[tuple[str, str]] = set()
    for assertion in spec.get("assertions", []):
        if not isinstance(assertion, dict) or assertion.get("kind") != "required":
            continue
        event = str(assertion.get("at") or "")
        check = assertion.get("check")
        if not event or not isinstance(check, list) or len(check) != 3:
            continue
        for operand in check[1:]:
            if not isinstance(operand, str) or not operand.startswith("$"):
                continue
            key = operand[1:]
            if "." in key:
                operand_event, field = key.split(".", 1)
                binding_key = key
            else:
                operand_event, field = event, key
                binding_key = f"{event}.{field}"
            if not field:
                continue
            if _is_literal_field(binding_key, _binding_expr(binding_key, bindings)):
                continue
            fields.add((operand_event, field))
    return fields


def _added_patch_lines(patch_text: str) -> list[str]:
    lines: list[str] = []
    for line in patch_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        lines.append(line[1:])
    return lines


def _added_statements(patch_text: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in _added_patch_lines(patch_text):
        if "ASSERT_EVT" not in line and not current:
            continue
        current.append(line.strip())
        if ";" in line:
            statements.append(" ".join(current))
            current = []
    if current:
        statements.append(" ".join(current))
    return statements


_C_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')


def _decode_c_string(token: str) -> str:
    try:
        return ast.literal_eval(token)
    except Exception:
        return token.strip('"')


def _format_and_arg_tail(statement: str) -> tuple[str, str] | None:
    first_match = None
    for match in _C_STRING_RE.finditer(statement):
        decoded = _decode_c_string(match.group(0))
        if "ASSERT_EVT" in decoded:
            first_match = match
            break
    if first_match is None:
        return None
    parts = [_decode_c_string(first_match.group(0))]
    end = first_match.end()
    while True:
        tail = statement[end:].lstrip()
        next_match = _C_STRING_RE.match(tail)
        if next_match is None:
            break
        parts.append(_decode_c_string(next_match.group(0)))
        end = len(statement) - len(tail) + next_match.end()
    return "".join(parts), statement[end:]


def _split_c_args(text: str) -> list[str]:
    text = text.strip()
    if text.startswith(","):
        text = text[1:]
    text = text.rsplit(")", 1)[0].rsplit(";", 1)[0].strip()
    args: list[str] = []
    current: list[str] = []
    depth = 0
    in_string = False
    quote = ""
    escape = False
    for char in text:
        if in_string:
            current.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in {'"', "'"}:
            in_string = True
            quote = char
            current.append(char)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
        if char == "," and depth == 0:
            arg = "".join(current).strip()
            if arg:
                args.append(arg)
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        args.append(tail)
    return args


_PRINTF_TOKEN_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")


def _strip_simple_casts(value: str) -> str:
    text = value.strip()
    changed = True
    while changed:
        changed = False
        next_text = re.sub(
            r"^\(\s*(?:const\s+)?(?:unsigned\s+|signed\s+)?(?:char|short|int|long|bool|size_t|ssize_t|uintptr_t|intptr_t|uint\d+_t|int\d+_t|void)\s*(?:\*+\s*)?\)\s*",
            "",
            text,
        ).strip()
        if next_text != text:
            text = next_text
            changed = True
    return text.strip()


def _is_static_literal_arg(value: str) -> bool:
    text = _norm(_strip_simple_casts(value))
    if text in _STATIC_LITERAL_VALUES:
        return True
    return bool(re.fullmatch(r"[-+]?(?:0x[0-9a-f]+|\d+)(?:[ul]+)?", text))


def validate_instrumentation_runtime_fields(
    *,
    spec: dict[str, Any],
    field_bindings: dict[str, Any],
    patch_text: str,
    patch_name: str = "instrumentation patch",
) -> dict[str, Any]:
    """Reject required non-literal fields emitted as hard-coded constants."""
    required = required_runtime_fields(spec, field_bindings)
    errors: list[str] = []
    checked: list[dict[str, str]] = []
    for statement in _added_statements(patch_text):
        parsed = _format_and_arg_tail(statement)
        if not parsed:
            continue
        fmt, arg_tail = parsed
        event_match = re.search(r"(?:^|\s)point=([A-Za-z_][A-Za-z0-9_]*)", fmt)
        if not event_match:
            continue
        event = event_match.group(1)
        args = _split_c_args(arg_tail)
        arg_index = 0
        for match in _PRINTF_TOKEN_RE.finditer(fmt):
            field = match.group(1)
            rendered = match.group(2)
            if field == "point":
                continue
            key = (event, field)
            if "%" in rendered:
                arg = args[arg_index] if arg_index < len(args) else ""
                arg_index += 1
                if key in required:
                    checked.append({"event": event, "field": field, "arg": arg})
                    if _is_static_literal_arg(arg):
                        errors.append(
                            f"{patch_name} hard-codes required runtime field "
                            f"${event}.{field} as {arg!r}; required assertion "
                            "fields must be measured from program state, not "
                            "printed as the expected answer"
                        )
            elif key in required and _norm(rendered) in _STATIC_LITERAL_VALUES:
                checked.append({"event": event, "field": field, "arg": rendered})
                errors.append(
                    f"{patch_name} hard-codes required runtime field "
                    f"${event}.{field} in the ASSERT_EVT format string as "
                    f"{rendered!r}; required assertion fields must be measured "
                    "from program state"
                )
    return {
        "valid": not errors,
        "required_fields": [
            f"${event}.{field}" for event, field in sorted(required)
        ],
        "checked": checked,
        "errors": errors,
    }
