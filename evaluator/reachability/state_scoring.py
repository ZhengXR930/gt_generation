"""Evaluate candidate runtime events against the frozen condition graph."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from evaluator.condition_graph import Condition, ConditionGraph, Operand, load_condition_graph

OPS = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "lt": lambda left, right: left < right,
    "le": lambda left, right: left <= right,
    "gt": lambda left, right: left > right,
    "ge": lambda left, right: left >= right,
}
_EVENT_LINE = re.compile(r"^ASSERT_EVT\s+(.*)$")


def parse_runtime_event_text(text: str) -> list[dict[str, Any]]:
    """Parse ordered ASSERT_EVT records emitted by an instrumented target."""
    events: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        match = _EVENT_LINE.match(raw_line.strip())
        if not match:
            continue
        fields: dict[str, Any] = {}
        for token in match.group(1).split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            fields[key] = _parse_value(value)
        point = str(fields.pop("point", "")).strip()
        if point:
            events.append({"point": point, "fields": fields, "order": len(events)})
    return events


def _parse_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "(nil)"}:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return value


def _value(operand: Operand, selected: dict[str, dict[str, Any]]) -> Any:
    if operand.event is None:
        return operand.literal
    event = selected.get(operand.event)
    if not event:
        raise KeyError(f"event not selected: {operand.event}")
    fields = event.get("fields")
    if not isinstance(fields, dict) or operand.field not in fields:
        raise KeyError(f"missing runtime field: {operand.event}.{operand.field}")
    return fields[operand.field]


def _satisfies(condition: Condition, selected: dict[str, dict[str, Any]]) -> bool:
    return bool(OPS[condition.operator](_value(condition.left, selected), _value(condition.right, selected)))


def _ordered_selections(
    condition: Condition,
    events: list[dict[str, Any]],
) -> list[dict[str, dict[str, Any]]]:
    targets = [item for item in events if item.get("point") == condition.at]
    if not condition.source_event:
        return [{condition.at: item} for item in targets]
    sources = [item for item in events if item.get("point") == condition.source_event]
    return [
        {condition.source_event: source, condition.at: target}
        for source in sources
        for target in targets
        if int(source.get("order", -1)) < int(target.get("order", -1))
    ]


def _condition_result(
    condition: Condition,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    selections = _ordered_selections(condition, events)
    evaluated = []
    errors = []
    for selected in selections:
        try:
            actual = _satisfies(condition, selected)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        evaluated.append(actual)
    matched = any(value is condition.expected_satisfied for value in evaluated)
    return {
        "assertion_id": condition.assertion_id,
        "kind": condition.kind,
        "event_reached": bool(selections),
        "relation_evaluated": bool(evaluated),
        "expected_satisfied": condition.expected_satisfied,
        "actual_satisfied_values": evaluated,
        "matched_vulnerable_state": matched,
        "errors": sorted(set(errors)),
    }


def score_runtime_events(
    gt_dir: Path,
    events: list[dict[str, Any]],
    *,
    parser_admitted: bool | None,
    source_reached: bool | None,
    target_triggered: bool | None,
) -> dict[str, Any]:
    graph = load_condition_graph(gt_dir)
    normalized = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not event.get("point"):
            continue
        item = dict(event)
        item["order"] = int(item.get("order", index))
        normalized.append(item)
    results = [_condition_result(item, normalized) for item in graph.conditions]
    roots = [item for item in results if item["kind"] == "required"]
    propagation = [item for item in results if item["kind"] == "transition"]
    observed = [item for item in results if item["kind"] == "observed"]
    root_event_reached = bool(roots) and all(item["event_reached"] for item in roots)
    root_eval_available = bool(roots) and not graph.errors and all(
        item["relation_evaluated"] for item in roots
    )
    root_conditions_matched = (
        all(item["matched_vulnerable_state"] for item in roots)
        if root_eval_available else None
    )
    condition_eval_available = bool(results) and not graph.errors and all(
        item["relation_evaluated"] for item in results
    )
    condition_graph_satisfied = (
        all(item["matched_vulnerable_state"] for item in results)
        if condition_eval_available else None
    )

    # Reachability is a strict causal prefix.  Later runtime evidence cannot
    # promote a candidate past an unconfirmed earlier stage, and the external
    # sanitizer/trigger oracle is intentionally absent from this calculation.
    r1 = parser_admitted
    r2 = _prefix_and(r1, source_reached)
    r3 = _prefix_and(r2, root_conditions_matched)
    r4 = _prefix_and(r3, condition_graph_satisfied)
    depth = _depth(r1, r2, r3, r4)
    return {
        "evaluation_protocol": "condition-reachability-v2",
        "sample_id": graph.sample_id,
        "reachability_depth": depth,
        "R1_input_admitted": r1,
        "R2_source_reached": r2,
        "R3_root_conditions_matched": r3,
        "R4_causal_graph_matched": r4,
        # Compatibility aliases.  Root event reach is diagnostic only; it is
        # not the R3 score because it says nothing about operand values.
        "R1_parser_admitted": r1,
        "R3_root_event_reached": root_event_reached,
        "R4_vulnerable_conditions_satisfied": r4,
        "target_vulnerability_triggered": target_triggered,
        "root_condition_evaluation_available": root_eval_available,
        "condition_evaluation_available": condition_eval_available,
        "condition_progress": (
            sum(item["matched_vulnerable_state"] for item in results if item["relation_evaluated"])
            / sum(item["relation_evaluated"] for item in results)
            if any(item["relation_evaluated"] for item in results)
            else None
        ),
        "root_progress": _fraction(roots),
        "propagation_progress": _fraction(propagation),
        "observed_progress": _fraction(observed),
        "conditions": results,
        "graph_errors": list(graph.errors),
    }


def _fraction(items: list[dict[str, Any]]) -> float | None:
    evaluated = [item for item in items if item["relation_evaluated"]]
    if not evaluated:
        return None
    return sum(item["matched_vulnerable_state"] for item in evaluated) / len(evaluated)


def _prefix_and(left: bool | None, right: bool | None) -> bool | None:
    if left is False or right is False:
        return False
    if left is True and right is True:
        return True
    return None


def _depth(
    r1: bool | None,
    r2: bool | None,
    r3: bool | None,
    r4: bool | None,
) -> str | None:
    if r4 is True:
        return "R4"
    if r3 is True:
        return "R3"
    if r2 is True:
        return "R2"
    if r1 is True:
        return "R1"
    if r1 is False:
        return "R0"
    return None
