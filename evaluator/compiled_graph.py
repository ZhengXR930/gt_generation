"""Compile one frozen GT package into the canonical evaluation graph.

The compiler deliberately contains no source search, prose similarity, or LLM
call.  It only joins the already-verified invariant, assertion, binding, event
location, and fine-trace records by their stable identities.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluator.condition_graph import Condition, ConditionGraph, Location, load_condition_graph


@dataclass(frozen=True)
class ReasoningAnchor:
    key: str
    location: Location
    line_end: int | None
    gt_step: int | None
    operand_roles: tuple[str, ...]
    minimum_roles: int


@dataclass(frozen=True)
class PropagationNode:
    invariant_id: str
    anchor: ReasoningAnchor


@dataclass(frozen=True)
class PropagationEdge:
    invariant_id: str
    source: ReasoningAnchor
    target: ReasoningAnchor


@dataclass(frozen=True)
class CompiledInvariantGraph:
    sample_id: str
    admission: Location
    source: ReasoningAnchor
    root: ReasoningAnchor
    nodes: tuple[PropagationNode, ...]
    edges: tuple[PropagationEdge, ...]
    runtime: ConditionGraph
    errors: tuple[str, ...]


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _location(value: Any) -> Location:
    item = value if isinstance(value, dict) else {}
    line = item.get("line")
    return Location(
        file=str(item.get("file") or ""),
        function=str(item.get("function") or ""),
        line=line if isinstance(line, int) else None,
    )


def _valid_location(value: Location) -> bool:
    return bool(value.file and value.function and isinstance(value.line, int))


def _split_roles(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    value = value.strip()
    if not value:
        return []
    # Split only top-level commas: ``current(), variant`` is two roles, while
    # ``strtol(value, &end, 10)`` is one coherent expression role.
    parts: list[str] = []
    start = depth = 0
    for index, character in enumerate(value):
        if character in "([{" :
            depth += 1
        elif character in ")]}" and depth:
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _condition_roles(condition: Condition, event: str) -> list[str]:
    roles = []
    for operand in (condition.left, condition.right):
        if operand.event == event and operand.source_expression:
            roles.append(operand.source_expression.strip())
    return roles


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _fine_step(gt: dict[str, Any], number: Any) -> dict[str, Any] | None:
    if not isinstance(number, int):
        return None
    return next(
        (
            item for item in gt.get("fine_trace", [])
            if isinstance(item, dict) and item.get("step") == number
        ),
        None,
    )


def _fallback_roles(gt: dict[str, Any], step: Any, variable: Any) -> list[str]:
    fine = _fine_step(gt, step)
    roles = _split_roles(fine.get("var")) if fine is not None else []
    if not roles:
        roles = _split_roles(variable)
    return roles


def _reasoning_roles(
    gt: dict[str, Any],
    step: Any,
    variable: Any,
    condition_roles: list[str],
) -> list[str]:
    """Prefer the GT fine-trace operand vocabulary used by subject traces.

    Assertion bindings remain the runtime truth contract.  They can contain a
    derived expression (for example ``len + 1``) that is intentionally not
    written at the root source statement, so they are only the fallback for
    reasoning localization.
    """
    return _fallback_roles(gt, step, variable) or condition_roles


def _fine_location(gt: dict[str, Any], step: Any) -> Location:
    return _location(_fine_step(gt, step))


def _fine_step_at_location(gt: dict[str, Any], location: Location) -> dict[str, Any] | None:
    def path_matches(left: str, right: Any) -> bool:
        a = left.replace("\\", "/").lstrip("./")
        b = str(right or "").replace("\\", "/").lstrip("./")
        return bool(a and b and (a == b or a.endswith("/" + b) or b.endswith("/" + a)))

    candidates = []
    for item in gt.get("fine_trace", []):
        if not isinstance(item, dict) or not path_matches(location.file, item.get("file")):
            continue
        if str(item.get("function") or "").split("(", 1)[0] != location.function.split("(", 1)[0]:
            continue
        start = item.get("line")
        end = item.get("line_end", start)
        if isinstance(start, int) and isinstance(end, int) and location.line is not None:
            if min(start, end) <= location.line <= max(start, end):
                return item
            candidates.append(item)
    if location.line is not None and candidates:
        return min(candidates, key=lambda item: abs(int(item["line"]) - location.line))
    return candidates[0] if candidates else None


def _complete_location(primary: Location, *fallbacks: Location) -> Location:
    if _valid_location(primary):
        return primary
    return next((item for item in fallbacks if _valid_location(item)), primary)


def _anchor(
    key: str,
    location: Location,
    step: Any,
    roles: list[str],
    *,
    minimum_roles: int | None = None,
    line_end: Any = None,
) -> ReasoningAnchor:
    unique = _dedupe(roles)
    required = len(unique) if minimum_roles is None else min(minimum_roles, len(unique))
    return ReasoningAnchor(
        key=key,
        location=location,
        line_end=line_end if isinstance(line_end, int) else location.line,
        gt_step=step if isinstance(step, int) else None,
        operand_roles=unique,
        minimum_roles=required,
    )


def _admission_location(gt: dict[str, Any]) -> Location:
    checkpoint = (gt.get("reachability_checkpoints") or {}).get("parser_admitted")
    if not isinstance(checkpoint, dict):
        checkpoint = gt.get("admitted_location")
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("admitted_location"), dict):
        checkpoint = checkpoint["admitted_location"]
    return _location(checkpoint)


def compile_invariant_graph(gt_dir: Path) -> CompiledInvariantGraph:
    gt = _load(gt_dir / "ground_truth.json")
    vi = _load(gt_dir / "verified_invariants.json")
    runtime = load_condition_graph(gt_dir)
    errors = list(runtime.errors)

    admission = _admission_location(gt)
    source_item = gt.get("source") or {}
    root_item = vi.get("root_cause_criterion") or {}
    root_gt = gt.get("root_cause") or {}
    source_location = _location(source_item)
    root_location = _location(root_item)

    root_id = str(root_item.get("invariant_id") or "")
    root_conditions = [
        condition for condition in runtime.root_conditions
        if root_id in condition.invariant_ids
    ] or list(runtime.root_conditions)
    assertion_root_roles: list[str] = []
    for condition in root_conditions:
        assertion_root_roles.extend(_condition_roles(condition, condition.at))
    root_step = root_item.get("fine_trace_step")
    if not isinstance(root_step, int):
        root_step = root_gt.get("trace_step")
    root_location = _complete_location(
        _fine_location(gt, root_step), root_location, _location(root_gt)
    )
    root_roles = _reasoning_roles(
        gt,
        root_step,
        root_item.get("variable") or root_gt.get("var"),
        assertion_root_roles,
    )

    source_roles = _split_roles(source_item.get("var"))
    source = _anchor(
        "source", source_location, source_item.get("trace_step"), source_roles,
        minimum_roles=1,
        line_end=(
            (_fine_step(gt, source_item.get("trace_step")) or {}).get("line_end")
            or source_item.get("line_end")
        ),
    )
    root = _anchor(
        root_id or "root",
        root_location,
        root_step,
        root_roles,
        line_end=(
            (_fine_step(gt, root_step) or {}).get("line_end")
            or root_item.get("line_end")
            or root_gt.get("line_end")
        ),
    )

    condition_by_invariant: dict[str, list[Condition]] = {}
    for condition in runtime.conditions:
        for invariant_id in condition.invariant_ids:
            condition_by_invariant.setdefault(invariant_id, []).append(condition)

    nodes: list[PropagationNode] = []
    for number, item in enumerate(vi.get("nodes", []), start=1):
        if not isinstance(item, dict) or item.get("verified") is False:
            continue
        invariant_id = str(item.get("invariant_id") or f"node-{number}")
        mapped_conditions = condition_by_invariant.get(invariant_id, [])
        condition_roles: list[str] = []
        for condition in mapped_conditions:
            condition_roles.extend(_condition_roles(condition, condition.at))
        roles = _reasoning_roles(
            gt,
            item.get("fine_trace_step"),
            item.get("variable"),
            condition_roles,
        )
        mapped_locations = [
            runtime.event_locations.get(condition.at, Location("", "", None))
            for condition in mapped_conditions
        ]
        inferred_fine = _fine_step_at_location(gt, _location(item))
        # Some verified runtime-only state assertions are intentionally more
        # detailed than the sparse GT fine trace.  They remain in the runtime
        # graph but cannot fairly become a reasoning unit without a trace
        # anchor.
        if not isinstance(item.get("fine_trace_step"), int) and inferred_fine is None:
            continue
        if not isinstance(item.get("fine_trace_step"), int) and inferred_fine is not None:
            roles = _split_roles(inferred_fine.get("var")) or roles
        node_location = _complete_location(
            _fine_location(gt, item.get("fine_trace_step")),
            _location(inferred_fine),
            _location(item),
            *mapped_locations,
        )
        node_anchor = _anchor(
            invariant_id,
            node_location,
            item.get("fine_trace_step"),
            roles,
            line_end=(
                (_fine_step(gt, item.get("fine_trace_step")) or inferred_fine or {}).get("line_end")
                or item.get("line_end")
            ),
        )
        nodes.append(PropagationNode(invariant_id, node_anchor))

    edges: list[PropagationEdge] = []
    for number, item in enumerate(vi.get("edges", []), start=1):
        if not isinstance(item, dict) or item.get("verified") is False:
            continue
        invariant_id = str(item.get("invariant_id") or f"edge-{number}")
        source_roles: list[str] = []
        target_roles: list[str] = []
        for condition in condition_by_invariant.get(invariant_id, []):
            if condition.kind != "transition":
                continue
            if condition.source_event:
                source_roles.extend(_condition_roles(condition, condition.source_event))
            target_roles.extend(_condition_roles(condition, condition.at))
        source_roles = _reasoning_roles(
            gt, item.get("from_step"), None, source_roles
        )
        target_roles = _reasoning_roles(
            gt, item.get("to_step"), None, target_roles
        )
        mapped_transitions = [
            condition for condition in condition_by_invariant.get(invariant_id, [])
            if condition.kind == "transition"
        ]
        source_event_locations = [
            runtime.event_locations.get(condition.source_event or "", Location("", "", None))
            for condition in mapped_transitions
        ]
        target_event_locations = [
            runtime.event_locations.get(condition.at, Location("", "", None))
            for condition in mapped_transitions
        ]
        raw_source_location = _location({
            "file": item.get("from_file"),
            "function": item.get("from_function"),
            "line": item.get("from_line"),
        })
        raw_target_location = _location({
            "file": item.get("to_file"),
            "function": item.get("to_function"),
            "line": item.get("to_line"),
        })
        inferred_source = _fine_step_at_location(gt, raw_source_location)
        inferred_target = _fine_step_at_location(gt, raw_target_location)
        if not isinstance(item.get("from_step"), int) and inferred_source is not None:
            source_roles = _split_roles(inferred_source.get("var")) or source_roles
        if not isinstance(item.get("to_step"), int) and inferred_target is not None:
            target_roles = _split_roles(inferred_target.get("var")) or target_roles
        source_location = _complete_location(
            _fine_location(gt, item.get("from_step")),
            _location(inferred_source),
            raw_source_location,
            *source_event_locations,
        )
        target_location = _complete_location(
            _fine_location(gt, item.get("to_step")),
            _location(inferred_target),
            raw_target_location,
            *target_event_locations,
        )
        edge_source = _anchor(
            f"{invariant_id}:from",
            source_location,
            item.get("from_step"),
            source_roles,
            line_end=(
                (_fine_step(gt, item.get("from_step")) or inferred_source or {}).get("line_end")
                or item.get("from_line_end")
            ),
        )
        edge_target = _anchor(
            f"{invariant_id}:to",
            target_location,
            item.get("to_step"),
            target_roles,
            line_end=(
                (_fine_step(gt, item.get("to_step")) or inferred_target or {}).get("line_end")
                or item.get("to_line_end")
            ),
        )
        edges.append(PropagationEdge(invariant_id, edge_source, edge_target))

    for name, location in (
        ("admission", admission), ("source", source.location), ("root", root.location)
    ):
        if not _valid_location(location):
            errors.append(f"missing complete {name} location")
    if not runtime.root_conditions:
        errors.append("missing root assertion")
    if not root.operand_roles:
        errors.append("missing root operand roles")
    for node in nodes:
        if not _valid_location(node.anchor.location):
            errors.append(f"{node.invariant_id}: missing node location")
    for edge in edges:
        if not _valid_location(edge.source.location):
            errors.append(f"{edge.invariant_id}: missing from location")
        if not _valid_location(edge.target.location):
            errors.append(f"{edge.invariant_id}: missing to location")
        mapped = condition_by_invariant.get(edge.invariant_id, [])
        if not any(condition.kind == "transition" for condition in mapped):
            errors.append(f"{edge.invariant_id}: missing transition assertion")

    return CompiledInvariantGraph(
        sample_id=str(gt.get("sample_id") or gt_dir.name),
        admission=admission,
        source=source,
        root=root,
        nodes=tuple(nodes),
        edges=tuple(edges),
        runtime=runtime,
        errors=tuple(sorted(set(errors))),
    )
