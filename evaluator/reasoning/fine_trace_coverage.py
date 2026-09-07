#!/usr/bin/env python3
"""Fine-grained trace coverage as node and edge recall.

The curated GT `fine_trace` is a DAG: each step is a node, and each entry in a
step's `depends_on` is an edge from the step it depends on. Coverage asks one
question of the subject's `analysis.json.fine_trace` — how much of that graph
did it recover?

- **node recall**: GT steps whose program point the subject also names.
- **edge recall**: GT dependencies whose two endpoints the subject recovered
  *and* placed in the same causal order. The subject schema carries no explicit
  `depends_on`, so step order is the dependency signal; an explicit
  `depends_on` is used instead when the subject supplies one.

A node is recovered or it is not: same file, same function, and a line inside
the GT step's range. There is no partial credit and no weighting, so a number
here means "this fraction of the causal graph was found", not a blended score.
Each subject step can satisfy at most one GT node, so restating one location
cannot cover several.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"
DEFAULT_LINE_TOLERANCE = 5
_STAGE_KEYS = ("parser", "source", "root_cause", "sink", "trigger")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class TraceNode:
    label: str
    file: str
    function: str
    line: int | None
    line_end: int | None = None
    role: str | None = None


def score_fine_trace_coverage(
    sample_id: str,
    analysis_artifact: dict[str, Any] | None,
    *,
    gt_dir: Path | None = None,
    line_tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> dict[str, Any]:
    """Score subject fine_trace node and edge recall against the curated GT DAG."""
    gt = json.loads(((gt_dir or (GT_RESULTS / sample_id)) / "ground_truth.json").read_text(encoding="utf-8"))
    gt_steps = [item for item in (gt.get("fine_trace") or []) if isinstance(item, dict)]
    gt_nodes = {_step_number(item, index): _node_from_gt(item, index) for index, item in enumerate(gt_steps, start=1)}
    gt_edges = _gt_edges(gt_steps)

    trace = (analysis_artifact or {}).get("fine_trace") if isinstance(analysis_artifact, dict) else None
    if not isinstance(trace, list):
        return {
            "evaluation_protocol": "fine-trace-coverage-v2",
            "sample_id": sample_id,
            "unavailable": "analysis.json fine_trace missing or invalid",
            "nodes": {"total": len(gt_nodes), "covered": 0, "recall": None},
            "edges": {"total": len(gt_edges), "covered": 0, "recall": None},
            "stage_coverage": {key: None for key in _STAGE_KEYS},
        }

    subject_steps = [item for item in trace if isinstance(item, dict)]
    assignment = _assign_nodes(gt_nodes, subject_steps, line_tolerance)
    covered_edges = [edge for edge in gt_edges if _edge_covered(edge, assignment, subject_steps)]

    return {
        "evaluation_protocol": "fine-trace-coverage-v2",
        "sample_id": sample_id,
        "line_tolerance": line_tolerance,
        "subject_steps_total": len(subject_steps),
        "nodes": {
            "total": len(gt_nodes),
            "covered": sum(1 for value in assignment.values() if value is not None),
            "recall": (sum(1 for value in assignment.values() if value is not None) / len(gt_nodes)) if gt_nodes else None,
        },
        "edges": {
            "total": len(gt_edges),
            "covered": len(covered_edges),
            "recall": (len(covered_edges) / len(gt_edges)) if gt_edges else None,
        },
        "stage_coverage": _stage_coverage(gt, subject_steps, line_tolerance),
        "node_reports": [
            {
                "gt_step": number,
                "target": gt_nodes[number].__dict__,
                "matched": assignment[number] is not None,
                "subject_index": assignment[number],
            }
            for number in sorted(gt_nodes)
        ],
        "edge_reports": [
            {**edge, "matched": edge in covered_edges}
            for edge in gt_edges
        ],
    }


def load_analysis_artifact(sample_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = sample_dir / "analysis.json"
    if not path.is_file():
        return None, "analysis.json missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"analysis.json invalid: {type(exc).__name__}: {exc}"
    return (value, None) if isinstance(value, dict) else (None, "analysis.json is not an object")


# --------------------------------------------------------------------------- graph


def _step_number(item: dict[str, Any], index: int) -> int:
    value = _int_or_none(item.get("step"))
    return value if value is not None else index


def _node_from_gt(item: dict[str, Any], index: int) -> TraceNode:
    return TraceNode(
        label=f"gt_step_{_step_number(item, index)}",
        file=str(item.get("file") or ""),
        function=str(item.get("function") or ""),
        line=_int_or_none(item.get("line")),
        line_end=_int_or_none(item.get("line_end")),
        role=str(item["role"]) if item.get("role") else None,
    )


def _gt_edges(gt_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for index, item in enumerate(gt_steps, start=1):
        target = _step_number(item, index)
        for dependency in item.get("depends_on") or []:
            if not isinstance(dependency, dict):
                continue
            source = _int_or_none(dependency.get("on"))
            if source is None:
                continue
            edges.append({"from": source, "to": target, "type": str(dependency.get("type") or "")})
    return edges


def _assign_nodes(
    gt_nodes: dict[int, TraceNode],
    subject_steps: list[dict[str, Any]],
    line_tolerance: int,
) -> dict[int, int | None]:
    """One GT node consumes at most one subject step, and vice versa."""
    assignment: dict[int, int | None] = {}
    taken: set[int] = set()
    for number in sorted(gt_nodes):
        node = gt_nodes[number]
        chosen = None
        for index, step in enumerate(subject_steps):
            if index in taken:
                continue
            if _node_matches(node, step, line_tolerance):
                chosen = index
                taken.add(index)
                break
        assignment[number] = chosen
    return assignment


def _edge_covered(edge: dict[str, Any], assignment: dict[int, int | None], subject_steps: list[dict[str, Any]]) -> bool:
    source_index = assignment.get(edge["from"])
    target_index = assignment.get(edge["to"])
    if source_index is None or target_index is None:
        return False
    explicit = _subject_dependencies(subject_steps, target_index)
    if explicit is not None:
        return _subject_step_number(subject_steps, source_index) in explicit
    return source_index < target_index


def _subject_dependencies(subject_steps: list[dict[str, Any]], index: int) -> set[int] | None:
    """Subject-declared dependencies, when the subject schema carries them."""
    raw = subject_steps[index].get("depends_on")
    if not isinstance(raw, list):
        return None
    values: set[int] = set()
    for item in raw:
        if isinstance(item, dict):
            value = _int_or_none(item.get("on"))
        else:
            value = _int_or_none(item)
        if value is not None:
            values.add(value)
    return values


def _subject_step_number(subject_steps: list[dict[str, Any]], index: int) -> int:
    value = _int_or_none(subject_steps[index].get("step"))
    return value if value is not None else (index + 1)


# --------------------------------------------------------------------------- stages


def _stage_coverage(gt: dict[str, Any], subject_steps: list[dict[str, Any]], line_tolerance: int) -> dict[str, bool | None]:
    coverage: dict[str, bool | None] = {}
    for key, node in _stage_nodes(gt).items():
        if node is None:
            coverage[key] = None
            continue
        coverage[key] = any(_node_matches(node, step, line_tolerance) for step in subject_steps)
    return coverage


def _stage_nodes(gt: dict[str, Any]) -> dict[str, TraceNode | None]:
    parser_payload = (gt.get("reachability_checkpoints") or {}).get("parser_admitted") or {}
    admitted = parser_payload.get("admitted_location") if isinstance(parser_payload, dict) else None
    parser = _node_from_anchor("parser", admitted if isinstance(admitted, dict) else parser_payload)

    sanitizer = gt.get("sanitizer_ground_truth") or {}
    trigger_payload: Any = None
    if isinstance(sanitizer, dict):
        for key in ("crash_location", "runtime_crash_location", "top_project_frame", "fault_location"):
            if isinstance(sanitizer.get(key), dict):
                trigger_payload = sanitizer[key]
                break
        if trigger_payload is None and any(key in sanitizer for key in ("file", "function", "line")):
            trigger_payload = sanitizer

    return {
        "parser": parser,
        "source": _node_from_anchor("source", gt.get("source")),
        "root_cause": _node_from_anchor("root_cause", gt.get("root_cause")),
        "sink": _node_from_anchor("sink", gt.get("sink")),
        "trigger": _node_from_anchor("trigger", trigger_payload),
    }


def _node_from_anchor(label: str, payload: Any) -> TraceNode | None:
    if not isinstance(payload, dict):
        return None
    if not any(payload.get(key) for key in ("file", "function", "line")):
        return None
    return TraceNode(
        label=label,
        file=str(payload.get("file") or ""),
        function=str(payload.get("function") or ""),
        line=_int_or_none(payload.get("line")),
        line_end=_int_or_none(payload.get("line_end")),
        role=label if label in {"source", "root_cause", "sink"} else None,
    )


# --------------------------------------------------------------------------- matching


def _node_matches(node: TraceNode, step: dict[str, Any], line_tolerance: int) -> bool:
    """A program point is recovered or it is not. No partial credit."""
    return (
        _path_equiv(node.file, step.get("file"))
        and _function_equiv(node.function, step.get("function"))
        and _line_in_range(node, _int_or_none(step.get("line")), line_tolerance)
    )


def _line_in_range(node: TraceNode, subject_line: int | None, tolerance: int) -> bool:
    if node.line is None:
        # GT gave no line: the location is the function, so file+function decides.
        return True
    if subject_line is None:
        return False
    upper = node.line_end if node.line_end is not None else node.line
    return (node.line - tolerance) <= subject_line <= (upper + tolerance)


def _path_equiv(left: Any, right: Any) -> bool:
    l, r = _norm_path(left), _norm_path(right)
    if not l or not r:
        return False
    return l == r or l.endswith("/" + r) or r.endswith("/" + l)


def _norm_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    for prefix in ("/workspace/repo-vul/src-vul/", "/workspace/", "/gt/_work/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    parts = [part for part in text.split("/") if part and part not in {".", "src-vul"}]
    return "/".join(parts)


def _function_equiv(left: Any, right: Any) -> bool:
    """Equal names, or an unqualified name naming the member of a qualified one.

    GT qualifies C++ members (``Parser::makeStream``) where a subject often
    writes just ``makeStream``. Two *differently* qualified names stay distinct:
    ``left::Parser::parse`` and ``right::Parser::parse`` are not the same
    function, and collapsing them would credit the wrong program point.
    """
    l, r = _norm_text(left), _norm_text(right)
    if not l or not r:
        return False
    if l == r:
        return True
    if "::" in l and "::" in r:
        return False
    qualified, plain = (l, r) if "::" in l else (r, l)
    return qualified.split("::")[-1] == plain


def _norm_text(value: Any) -> str:
    return _SPACE_RE.sub("", str(value or "").strip())


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
