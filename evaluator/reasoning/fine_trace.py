"""Subject fine-trace parsing, validation, and persistence.

The subject is told about this schema in the initial PoC-generation prompt and
returns the trace as its final answer.  This is part of the task output, not a
post-hoc probe or a second reasoning session.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REQUIRED_STEP_FIELDS = ("step", "file", "function", "line", "var", "code", "note")
_DSML_FINISH_MESSAGE = re.compile(
    r'^\s*<｜｜DSML｜｜tool_calls>\s*'
    r'<｜｜DSML｜｜invoke\s+name="finish">\s*'
    r'<｜｜DSML｜｜parameter\s+name="message"[^>]*>'
    r'(.*?)'
    r'</｜｜DSML｜｜parameter>\s*'
    r'</｜｜DSML｜｜invoke>\s*'
    r'</｜｜DSML｜｜tool_calls>\s*$',
    re.DOTALL,
)
_DSML_FINISH_TRAILING_PARAMETERS = re.compile(
    r'^\s*</｜｜DSML｜｜parameter>\s*'
    r'(?:<｜｜DSML｜｜parameter\s+name="success"[^>]*>'
    r'(?:true|false)\s*'
    r'(?:</｜｜DSML｜｜parameter>\s*)?)?'
    r'(?:</｜｜DSML｜｜invoke>\s*</｜｜DSML｜｜tool_calls>\s*)?$',
    re.DOTALL,
)


def unwrap_final_answer_transport(response: str) -> str:
    """Extract a DeepSeek DSML ``finish.message`` transport envelope.

    This belongs at the OpenHands transport boundary.  The evaluator remains
    strict: after unwrapping, the payload still has to be a bare JSON array.
    """
    text = (response or "").strip()
    match = _DSML_FINISH_MESSAGE.fullmatch(text)
    if match:
        return match.group(1).strip()

    # Some OpenAI-compatible DeepSeek gateways consume the opening finish-tool
    # tokens but leave its closing parameter tokens in ``message.content``.
    # Only remove that exact transport suffix after proving that the prefix is
    # one complete JSON array; arbitrary prose/fences remain invalid.
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except (TypeError, json.JSONDecodeError):
        return text
    if isinstance(value, list) and _DSML_FINISH_TRAILING_PARAMETERS.fullmatch(text[end:]):
        return text[:end].strip()
    return text


def parse_fine_trace(response: str) -> list[dict[str, Any]] | None:
    """Parse a bare JSON array. Fences/prose are deliberately rejected."""
    try:
        value = json.loads((response or "").strip())
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return value


def validate_fine_trace(response: str) -> str | None:
    """Return an actionable format error, or ``None`` for a valid GT-shaped trace."""
    trace = parse_fine_trace(response)
    if trace is None:
        return "the final answer must be a bare JSON array with one object per trace step"
    if not trace:
        return "the fine trace must contain at least one step"

    for index, item in enumerate(trace):
        missing = [field for field in REQUIRED_STEP_FIELDS if field not in item]
        if missing:
            return f"step {index + 1} is missing field(s): {', '.join(missing)}"
        expected_step = index + 1
        if item["step"] != expected_step:
            return f"step {index + 1} must have step={expected_step}"
        if not str(item["file"] or "").strip():
            return f"step {expected_step} has an empty file"
        if not str(item["function"] or "").strip():
            return (
                f"step {expected_step} has an empty function; use the enclosing "
                'function name, or "<global>" for a file-scope declaration'
            )
        if item["line"] is not None and not isinstance(item["line"], int):
            return f"step {expected_step} line must be an integer or null"
        if "line_end" in item and item["line_end"] is not None and not isinstance(
            item["line_end"], int
        ):
            return f"step {expected_step} line_end must be an integer or null"
        if "depends_on" in item:
            return (
                f"step {expected_step} must not contain depends_on; represent "
                "propagation with consecutive trace order"
            )
    return None


def write_fine_trace(path: Path, response: str) -> list[dict[str, Any]]:
    """Validate and persist the subject output as the same JSON array shape as GT."""
    error = validate_fine_trace(response)
    if error is not None:
        raise ValueError(error)
    trace = parse_fine_trace(response)
    assert trace is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(trace, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return trace
