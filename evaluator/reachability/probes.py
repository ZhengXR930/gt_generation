"""Runtime probe contract for value-capturing condition evaluation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluator.condition_graph import ConditionGraph


def compile_runtime_probes(
    graph: ConditionGraph,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build ephemeral GDB value probes from the frozen assertion bindings.

    Probes are evaluation artifacts, not GT package artifacts.  The source
    expressions are already bound in ``field_bindings.json`` and are joined to
    verified event locations here without an LLM or source-code guess.
    """
    captures_by_event: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for condition in graph.conditions:
        for operand in (condition.left, condition.right):
            if not operand.event or not operand.field:
                continue
            expression = operand.source_expression.strip()
            if not expression:
                errors.append(
                    f"missing capture expression {operand.event}.{operand.field}"
                )
                continue
            captures_by_event.setdefault(operand.event, {})[operand.field] = expression
    checkpoints = []
    for point, captures in captures_by_event.items():
        location = graph.event_locations.get(point)
        if location is None or not location.file or not location.function or location.line is None:
            errors.append(f"missing runtime location {point}")
            continue
        checkpoints.append({
            "kind": "condition_event",
            "event_point": point,
            "file": location.file,
            "function": location.function,
            "line": location.line,
            "captures": captures,
        })
    return checkpoints, sorted(set(errors))


def load_runtime_probes(
    path: Path,
    graph: ConditionGraph,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], ["runtime_probes.json missing"]
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, dict):
        return [], ["runtime_probes.json events must be an object"]
    checkpoints = []
    errors = []
    required_fields: dict[str, set[str]] = {}
    for condition in graph.conditions:
        for operand in (condition.left, condition.right):
            if operand.event and operand.field:
                required_fields.setdefault(operand.event, set()).add(operand.field)
    for point, fields in required_fields.items():
        spec = events.get(point)
        if not isinstance(spec, dict):
            errors.append(f"missing runtime probe event {point}")
            continue
        captures = spec.get("captures")
        if not isinstance(captures, dict):
            errors.append(f"runtime probe {point} captures must be an object")
            continue
        missing = sorted(fields - set(captures))
        if missing:
            errors.append(
                f"runtime probe {point} missing fields: {', '.join(missing)}"
            )
        location = graph.event_locations.get(point)
        checkpoints.append({
            "kind": "condition_event",
            "event_point": point,
            "file": str(spec.get("file") or (location.file if location else "")),
            "function": str(
                spec.get("function") or (location.function if location else "")
            ),
            "line": spec.get("line") or (location.line if location else None),
            "captures": {
                str(name): str(expression)
                for name, expression in captures.items()
                if str(expression).strip()
            },
        })
    return checkpoints, sorted(set(errors))
