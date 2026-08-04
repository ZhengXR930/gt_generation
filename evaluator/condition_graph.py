"""Compile frozen GT assertions into one deterministic evaluation contract.

The condition graph is shared by the reasoning and runtime evaluators.  It is
important that both consumers use the same assertion identities, operands,
operators, event locations, and vulnerable-reference truth values.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

LITERAL_SUFFIXES = {
    "null_literal": None,
    "zero_literal": 0,
    "true_literal": True,
    "false_literal": False,
    # libxml2's xmlParserInputState defines XML_PARSER_EOF as -1.  The frozen
    # assertion uses a named literal so it remains source-readable; it is a
    # compile-time operand, not a runtime event field.
    "eof_literal": -1,
}
OPS = {"eq", "ne", "lt", "le", "gt", "ge"}


@dataclass(frozen=True)
class Location:
    file: str
    function: str
    line: int | None


@dataclass(frozen=True)
class Operand:
    raw: Any
    event: str | None
    field: str | None
    source_expression: str
    literal: Any = None


@dataclass(frozen=True)
class Condition:
    assertion_id: str
    invariant_ids: tuple[str, ...]
    kind: str
    operator: str
    left: Operand
    right: Operand
    at: str
    source_event: str | None
    protects: str | None
    expected_satisfied: bool
    expected_status: str


@dataclass(frozen=True)
class ConditionGraph:
    sample_id: str
    source: Location
    root: Location
    sink: Location
    event_locations: dict[str, Location]
    conditions: tuple[Condition, ...]
    errors: tuple[str, ...]

    @property
    def root_conditions(self) -> tuple[Condition, ...]:
        return tuple(item for item in self.conditions if item.kind == "required")

    @property
    def propagation_conditions(self) -> tuple[Condition, ...]:
        return tuple(item for item in self.conditions if item.kind == "transition")

    @property
    def observed_conditions(self) -> tuple[Condition, ...]:
        return tuple(item for item in self.conditions if item.kind == "observed")


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _location(value: Any) -> Location:
    item = value if isinstance(value, dict) else {}
    line = item.get("line")
    return Location(
        file=str(item.get("file") or ""),
        function=str(item.get("function") or ""),
        line=line if isinstance(line, int) else None,
    )


def _operand(
    value: Any,
    bindings: dict[str, str],
    *,
    default_event: str,
) -> Operand:
    if not isinstance(value, str) or not value.startswith("$"):
        return Operand(
            raw=value,
            event=None,
            field=None,
            source_expression=json.dumps(value, ensure_ascii=False),
            literal=value,
        )
    key = value[1:]
    event, separator, field = key.partition(".")
    if not separator:
        if key not in LITERAL_SUFFIXES:
            expression = (
                bindings.get(f"{default_event}.{key}")
                or bindings.get(key)
                or ""
            )
            return Operand(
                raw=value,
                event=default_event or None,
                field=key,
                source_expression=str(expression),
                literal=None,
            )
        literal = LITERAL_SUFFIXES[key]
        return Operand(
            raw=value,
            event=None,
            field=None,
            source_expression=str(bindings.get(key) or literal or ""),
            literal=literal,
        )
    if field in LITERAL_SUFFIXES:
        literal = LITERAL_SUFFIXES[field]
        return Operand(
            raw=value,
            event=None,
            field=None,
            source_expression=str(bindings.get(key) or field),
            literal=literal,
        )
    literal = None
    expression = bindings.get(key) or ""
    return Operand(
        raw=value,
        event=event or None,
        field=field or None,
        source_expression=str(expression),
        literal=literal,
    )


def load_condition_graph(gt_dir: Path) -> ConditionGraph:
    gt = _load(gt_dir / "ground_truth.json")
    invariants = _load(gt_dir / "verified_invariants.json")
    assertions = _load(gt_dir / "verified_assertions.json")
    results = _load(gt_dir / "assertion_results.json")
    bindings = (_load(gt_dir / "field_bindings.json").get("bindings") or {})
    locations_raw = (_load(gt_dir / "event_locations.json").get("locations") or {})
    event_locations = {
        str(name): _location(value)
        for name, value in locations_raw.items()
        if isinstance(value, dict)
    }
    _complete_event_locations(
        event_locations=event_locations,
        assertions=assertions,
        invariants=invariants,
        gt=gt,
    )
    result_by_id = {
        str(item.get("id")): item
        for item in results.get("assertions", [])
        if isinstance(item, dict) and item.get("id")
    }
    original_case = str(results.get("original_case") or "original")
    errors: list[str] = []
    conditions: list[Condition] = []
    for item in assertions.get("assertions", []):
        if not isinstance(item, dict):
            continue
        assertion_id = str(item.get("id") or "")
        kind = str(item.get("kind") or "")
        check = item.get("check")
        if (
            not assertion_id
            or kind not in {"required", "transition", "observed"}
            or not isinstance(check, list)
            or len(check) != 3
            or check[0] not in OPS
        ):
            errors.append(f"invalid assertion schema: {assertion_id or '<missing>'}")
            continue
        at = str(item.get("at") or "")
        source_event = str(item.get("from") or "") or None
        protects = str(item.get("protects") or "") or None
        for point in (at, source_event, protects):
            if point and point not in event_locations:
                errors.append(f"{assertion_id}: missing event location {point}")
        left = _operand(check[1], bindings, default_event=at)
        right = _operand(check[2], bindings, default_event=at)
        # A few compact GT packages use a semantic operand namespace (for
        # example ``coverage_source``) for a value captured at the assertion's
        # actual event.  Resolve that alias deterministically to the only
        # executable endpoint; the original field binding/expression remains
        # unchanged.
        if left.event and left.event not in event_locations:
            left = replace(
                left,
                event=(source_event if kind == "transition" and source_event else at),
            )
        if right.event and right.event not in event_locations:
            right = replace(right, event=at)
        for operand in (left, right):
            if operand.event and not operand.source_expression:
                errors.append(
                    f"{assertion_id}: missing source binding "
                    f"{operand.event}.{operand.field}"
                )
        result = result_by_id.get(assertion_id, {})
        cell = (
            ((result.get("matrix") or {}).get("vulnerable") or {})
            .get(original_case, {})
        )
        expected = cell.get("satisfied")
        if not isinstance(expected, bool):
            errors.append(f"{assertion_id}: missing vulnerable reference truth value")
            continue
        conditions.append(
            Condition(
                assertion_id=assertion_id,
                invariant_ids=tuple(str(x) for x in item.get("invariants", [])),
                kind=kind,
                operator=str(check[0]),
                left=left,
                right=right,
                at=at,
                source_event=source_event,
                protects=protects,
                expected_satisfied=expected,
                expected_status=str(cell.get("status") or ""),
            )
        )
    return ConditionGraph(
        sample_id=str(gt.get("sample_id") or gt_dir.name),
        source=_location(gt.get("source")),
        root=_location(gt.get("root_cause")),
        sink=_location(gt.get("sink")),
        event_locations=event_locations,
        conditions=tuple(conditions),
        errors=tuple(sorted(set(errors))),
    )


def _complete_event_locations(
    *,
    event_locations: dict[str, Location],
    assertions: dict[str, Any],
    invariants: dict[str, Any],
    gt: dict[str, Any],
) -> None:
    """Fill durable event locations from already-verified invariant anchors.

    Some compacted packages lost redundant event-location entries while their
    verified edge/node endpoint locations remained intact.  This is a
    deterministic reconstruction, not a heuristic source search.
    """
    edges = {
        str(item.get("invariant_id")): item
        for item in invariants.get("edges", [])
        if isinstance(item, dict) and item.get("invariant_id")
    }
    nodes = {
        str(item.get("invariant_id")): item
        for item in invariants.get("nodes", [])
        if isinstance(item, dict) and item.get("invariant_id")
    }
    root = invariants.get("root_cause_criterion")
    root_id = str(root.get("invariant_id") or "") if isinstance(root, dict) else ""
    for assertion in assertions.get("assertions", []):
        if not isinstance(assertion, dict):
            continue
        ids = [str(value) for value in assertion.get("invariants", [])]
        edge = next((edges[value] for value in ids if value in edges), None)
        at = str(assertion.get("at") or "")
        source = str(assertion.get("from") or "")
        protects = str(assertion.get("protects") or "")
        if edge is not None:
            if at and at not in event_locations:
                event_locations[at] = _location({
                    "file": edge.get("to_file"),
                    "function": edge.get("to_function"),
                    "line": edge.get("to_line"),
                })
            if source and source not in event_locations:
                event_locations[source] = _location({
                    "file": edge.get("from_file"),
                    "function": edge.get("from_function"),
                    "line": edge.get("from_line"),
                })
        if at and at not in event_locations:
            anchor = next((nodes[value] for value in ids if value in nodes), None)
            if anchor is None and root_id in ids:
                anchor = root
            if isinstance(anchor, dict):
                event_locations[at] = _location(anchor)
        if protects and protects not in event_locations:
            event_locations[protects] = _location(gt.get("sink"))


def graph_to_json(graph: ConditionGraph) -> dict[str, Any]:
    def loc(value: Location) -> dict[str, Any]:
        return {"file": value.file, "function": value.function, "line": value.line}

    def operand(value: Operand) -> dict[str, Any]:
        return {
            "raw": value.raw,
            "event": value.event,
            "field": value.field,
            "source_expression": value.source_expression,
            "literal": value.literal,
        }

    return {
        "schema_version": "condition-graph-v1",
        "sample_id": graph.sample_id,
        "source": loc(graph.source),
        "root": loc(graph.root),
        "sink": loc(graph.sink),
        "event_locations": {
            name: loc(value) for name, value in graph.event_locations.items()
        },
        "conditions": [
            {
                "assertion_id": item.assertion_id,
                "invariant_ids": list(item.invariant_ids),
                "kind": item.kind,
                "operator": item.operator,
                "left": operand(item.left),
                "right": operand(item.right),
                "at": item.at,
                "from": item.source_event,
                "protects": item.protects,
                "expected_satisfied": item.expected_satisfied,
                "expected_status": item.expected_status,
            }
            for item in graph.conditions
        ],
        "errors": list(graph.errors),
    }
