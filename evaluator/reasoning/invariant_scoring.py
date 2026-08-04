#!/usr/bin/env python3
"""Non-LLM reasoning evaluation against verified invariant anchors.

Free-form ``note`` text is never read.  A match requires a structured source
location and a coherent set of operand roles in one fine-trace step.  A
propagation edge additionally requires its two endpoint steps in runtime order.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable

from evaluator.compiled_graph import (
    CompiledInvariantGraph,
    PropagationEdge,
    ReasoningAnchor,
    compile_invariant_graph,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"
DEFAULT_LINE_TOLERANCE = 0

_C_WORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if",
    "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
    "unsigned", "void", "volatile", "while", "true", "false", "null",
}


def _norm_path(value: Any) -> str:
    path = str(value or "").replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return re.sub(r"/+", "/", path)


def _file_matches(left: Any, right: Any) -> bool:
    a, b = _norm_path(left), _norm_path(right)
    return bool(a and b and (a == b or a.endswith("/" + b) or b.endswith("/" + a)))


def _norm_function(value: Any) -> str:
    prefix = str(value or "").strip().split("(", 1)[0].strip()
    words = prefix.split()
    return words[-1] if words else ""


def _tokens(value: Any) -> set[str]:
    found = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(value or "")))
    return {token for token in found if token.lower() not in _C_WORDS}


def _line_matches(anchor: ReasoningAnchor, step: dict[str, Any], tolerance: int) -> bool:
    anchor_start = anchor.location.line
    if anchor_start is None:
        return False
    anchor_end = anchor.line_end if isinstance(anchor.line_end, int) else anchor_start
    step_start = step.get("line")
    if not isinstance(step_start, int):
        return False
    step_end = step.get("line_end")
    if not isinstance(step_end, int):
        step_end = step_start
    anchor_start, anchor_end = min(anchor_start, anchor_end), max(anchor_start, anchor_end)
    step_start, step_end = min(step_start, step_end), max(step_start, step_end)
    return (
        step_start - tolerance <= anchor_end
        and anchor_start <= step_end + tolerance
    )


def _location_matches(anchor: ReasoningAnchor, step: dict[str, Any], tolerance: int) -> bool:
    return (
        _file_matches(anchor.location.file, step.get("file"))
        and _norm_function(anchor.location.function) == _norm_function(step.get("function"))
        and _line_matches(anchor, step, tolerance)
    )


def _role_matches(role: str, step_tokens: set[str]) -> bool:
    role_tokens = _tokens(role)
    return bool(role_tokens) and role_tokens <= step_tokens


def _coherent(anchor: ReasoningAnchor, step: dict[str, Any]) -> bool:
    if not anchor.operand_roles:
        return anchor.minimum_roles == 0
    step_tokens = _tokens(step.get("var")) | _tokens(step.get("code"))
    matched = sum(_role_matches(role, step_tokens) for role in anchor.operand_roles)
    return matched >= anchor.minimum_roles


def _candidates(
    anchor: ReasoningAnchor,
    trace: list[dict[str, Any]],
    *,
    tolerance: int,
    coherent: bool,
) -> list[int]:
    return [
        index for index, step in enumerate(trace)
        if _location_matches(anchor, step, tolerance)
        and (not coherent or _coherent(anchor, step))
    ]


def _same_location(left: ReasoningAnchor, right: ReasoningAnchor) -> bool:
    return (
        _file_matches(left.location.file, right.location.file)
        and _norm_function(left.location.function) == _norm_function(right.location.function)
        and left.location.line == right.location.line
        and left.line_end == right.line_end
    )


def _edge_match(
    edge: PropagationEdge,
    trace: list[dict[str, Any]],
    *,
    tolerance: int,
    coherent: bool,
) -> tuple[bool, tuple[int, int] | None]:
    sources = _candidates(edge.source, trace, tolerance=tolerance, coherent=coherent)
    targets = _candidates(edge.target, trace, tolerance=tolerance, coherent=coherent)
    allow_same = _same_location(edge.source, edge.target)
    pairs = [
        (source, target)
        for source in sources
        for target in targets
        if source < target or (allow_same and source == target)
    ]
    return (bool(pairs), min(pairs) if pairs else None)


def score_compiled_trace(
    graph: CompiledInvariantGraph,
    trace: list[dict[str, Any]],
    *,
    line_tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> dict[str, Any]:
    steps = [item for item in trace if isinstance(item, dict)]
    source_location = _candidates(
        graph.source, steps, tolerance=line_tolerance, coherent=False
    )
    source_matches = _candidates(
        graph.source, steps, tolerance=line_tolerance, coherent=True
    )
    root_location = _candidates(
        graph.root, steps, tolerance=line_tolerance, coherent=False
    )
    root_matches = _candidates(
        graph.root, steps, tolerance=line_tolerance, coherent=True
    )

    node_rows = []
    for node in graph.nodes:
        location = _candidates(
            node.anchor, steps, tolerance=line_tolerance, coherent=False
        )
        coherent = _candidates(
            node.anchor, steps, tolerance=line_tolerance, coherent=True
        )
        node_rows.append({
            "invariant_id": node.invariant_id,
            "matched": bool(coherent),
            "location_matches": _one_based(location),
            "coherent_matches": _one_based(coherent),
            "anchor": _anchor_json(node.anchor),
        })

    edge_rows = []
    for edge in graph.edges:
        location_ok, location_pair = _edge_match(
            edge, steps, tolerance=line_tolerance, coherent=False
        )
        coherent_ok, coherent_pair = _edge_match(
            edge, steps, tolerance=line_tolerance, coherent=True
        )
        edge_rows.append({
            "invariant_id": edge.invariant_id,
            "matched": coherent_ok,
            "ordered_location_match": location_ok,
            "location_pair": _pair_json(location_pair),
            "coherent_pair": _pair_json(coherent_pair),
            "source": _anchor_json(edge.source),
            "target": _anchor_json(edge.target),
        })

    units = node_rows + edge_rows
    propagation_score = (
        sum(bool(item["matched"]) for item in units) / len(units)
        if units else None
    )
    return {
        "evaluation_protocol": "ordered-invariant-reasoning-v2",
        "sample_id": graph.sample_id,
        "trace_steps": len(steps),
        "line_tolerance": line_tolerance,
        "source_score": int(bool(source_matches)),
        "root_cause_score": int(bool(root_matches)),
        "propagation_score": propagation_score,
        "propagation_exact": (
            all(bool(item["matched"]) for item in units) if units else None
        ),
        "graph_errors": list(graph.errors),
        "diagnostics": {
            "source": {
                "anchor": _anchor_json(graph.source),
                "location_matches": _one_based(source_location),
                "coherent_matches": _one_based(source_matches),
            },
            "root_cause": {
                "anchor": _anchor_json(graph.root),
                "location_matches": _one_based(root_location),
                "coherent_matches": _one_based(root_matches),
            },
            "propagation": {
                "matched_units": sum(bool(item["matched"]) for item in units),
                "total_units": len(units),
                "nodes": node_rows,
                "edges": edge_rows,
            },
        },
    }


def score_invariant_trace(
    sample_id: str,
    trace: list[dict[str, Any]],
    *,
    line_tolerance: int = DEFAULT_LINE_TOLERANCE,
    gt_dir: Path | None = None,
) -> dict[str, Any]:
    graph = compile_invariant_graph(gt_dir or (GT_RESULTS / sample_id))
    return score_compiled_trace(graph, trace, line_tolerance=line_tolerance)


def _anchor_json(anchor: ReasoningAnchor) -> dict[str, Any]:
    return {
        "key": anchor.key,
        "file": anchor.location.file,
        "function": anchor.location.function,
        "line": anchor.location.line,
        "line_end": anchor.line_end,
        "gt_step": anchor.gt_step,
        "operand_roles": list(anchor.operand_roles),
        "minimum_roles": anchor.minimum_roles,
    }


def _one_based(indices: Iterable[int]) -> list[int]:
    return [index + 1 for index in indices]


def _pair_json(pair: tuple[int, int] | None) -> list[int] | None:
    return [pair[0] + 1, pair[1] + 1] if pair else None


def _discover(models: list[str]) -> list[tuple[str, str, Path]]:
    rows = []
    for model in models:
        for path in sorted((POC_RESULTS / model).glob("*/fine_trace.json")):
            if (GT_RESULTS / path.parent.name / "verified_invariants.json").is_file():
                rows.append((model, path.parent.name, path))
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--sample-id", action="append")
    parser.add_argument("--line-tolerance", type=int, default=DEFAULT_LINE_TOLERANCE)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    selected = set(args.sample_id or [])
    rows = []
    for model, sample_id, path in _discover(args.model):
        if selected and sample_id not in selected:
            continue
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(trace, list):
                raise ValueError("fine trace is not a JSON array")
            row = score_invariant_trace(
                sample_id, trace, line_tolerance=args.line_tolerance
            )
            row["model"] = model
            rows.append(row)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            rows.append({"model": model, "sample_id": sample_id, "error": f"{type(exc).__name__}: {exc}"})
    for model in args.model:
        available = [row for row in rows if row.get("model") == model and "error" not in row]
        if available:
            print(
                f"{model}: n={len(available)} "
                f"source={_mean(available, 'source_score'):.3f} "
                f"root={_mean(available, 'root_cause_score'):.3f} "
                f"propagation={_mean(available, 'propagation_score'):.3f}"
            )
    if args.out:
        args.out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _mean(rows: list[dict[str, Any]], key: str) -> float:
    values = [row[key] for row in rows if row.get(key) is not None]
    return sum(values) / len(values) if values else float("nan")


if __name__ == "__main__":
    main()
