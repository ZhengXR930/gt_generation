#!/usr/bin/env python3
"""Aggregate PoC-generation evaluation metrics across model result directories.

This script is intentionally read-only with respect to sample artifacts.  It
scores the existing analysis/context/reachability files and writes aggregate
reports under ``poc_generation/poc_results`` by default.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluator.reasoning.analysis_artifact import (  # noqa: E402
    validate_analysis_artifact,
    validate_analysis_artifact_quality,
)
from evaluator.reasoning.context_recall import score_context_recall  # noqa: E402
from evaluator.reasoning.fine_trace_coverage import score_fine_trace_coverage  # noqa: E402
from evaluator.reasoning.vuln_logic_scoring import score_vuln_logic  # noqa: E402


RESULTS_ROOT = REPO_ROOT / "poc_generation" / "poc_results"
DEFAULT_SELECTION = REPO_ROOT / "dataset" / "selected_500.json"
DEFAULT_EXCLUDE = {"_archived", "_batch_logs", "evaluation_rerun_queues_20261006", "test"}
SAMPLE_PREFIXES = ("arvo_", "osv_", "secbench_", "nvd_")
REACH_KEYS = (
    "R1_input_admitted",
    "R2_source_reached",
    "R3_root_cause_reached",
    "R4_sink_reached",
)
STAGE_KEYS = ("parser", "source", "root_cause", "sink", "trigger")
LOGIC_SCALAR_KEYS = (
    "source_location_score",
    "source_operand_score",
    "root_cause_location_score",
    "root_cause_operand_score",
    "root_cause_relation_score",
    "sink_location_score",
    "sink_operand_score",
    "sink_relation_score",
    "propagation_node_recall_score",
    "propagation_node_precision_score",
    "propagation_ordered_coverage_score",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate existing PoC-generation evaluation artifacts."
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=RESULTS_ROOT,
        help="Root containing one directory per evaluated model.",
    )
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        help="Model/result directory to include. Repeatable. Defaults to all active dirs.",
    )
    parser.add_argument(
        "--selection",
        type=Path,
        default=DEFAULT_SELECTION,
        help=(
            "JSON sample selection for formal comparison. Defaults to "
            "dataset/selected_500.json."
        ),
    )
    parser.add_argument(
        "--include-incomplete-models",
        action="store_true",
        help=(
            "When discovering models automatically, include result dirs that "
            "do not exactly match the selected sample set."
        ),
    )
    parser.add_argument(
        "--line-tolerance",
        type=int,
        default=5,
        help="Ground-truth line tolerance for reasoning location matching.",
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        help="Output path prefix without extension. Defaults to results_root/evaluation_metrics_<timestamp>.",
    )
    parser.add_argument(
        "--no-latest",
        action="store_true",
        help="Do not refresh evaluation_metrics_latest.* convenience copies.",
    )
    args = parser.parse_args()

    results_root = args.results_root.resolve()
    output_prefix = args.output_prefix
    if output_prefix is None:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")
        output_prefix = results_root / f"evaluation_metrics_{stamp}"
    elif not output_prefix.is_absolute():
        output_prefix = (Path.cwd() / output_prefix).resolve()

    selection_path = args.selection
    if not selection_path.is_absolute():
        selection_path = (Path.cwd() / selection_path).resolve()
    selected_sample_ids = load_selection_ids(selection_path)

    models = args.models or discover_models(
        results_root,
        selected_sample_ids,
        include_incomplete=args.include_incomplete_models,
    )
    report = evaluate_models(
        results_root,
        models,
        line_tolerance=args.line_tolerance,
        selected_sample_ids=selected_sample_ids,
        selection_path=selection_path,
        include_incomplete_models=args.include_incomplete_models,
    )

    write_reports(report, output_prefix)
    if not args.no_latest:
        write_reports(report, results_root / "evaluation_metrics_latest")

    print(json.dumps({
        "models": len(report["models"]),
        "summary_csv": str(output_prefix.with_suffix(".summary.csv")),
        "per_sample_csv": str(output_prefix.with_suffix(".per_sample.csv")),
        "json": str(output_prefix.with_suffix(".json")),
    }, ensure_ascii=False, indent=2))
    return 0


def load_selection_ids(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"selection must be a JSON list: {path}")
    sample_ids: list[str] = []
    for item in data:
        sample_id = item.get("sample_id") if isinstance(item, dict) else item
        if not isinstance(sample_id, str) or not sample_id:
            raise ValueError(f"selection entry is missing sample_id: {path}")
        sample_ids.append(sample_id)
    unique = set(sample_ids)
    if len(unique) != len(sample_ids):
        raise ValueError(f"selection contains duplicate sample IDs: {path}")
    return unique


def model_sample_ids(model_dir: Path) -> set[str]:
    return {
        path.name
        for path in model_dir.iterdir()
        if path.is_dir() and path.name.startswith(SAMPLE_PREFIXES)
    }


def discover_models(
    results_root: Path,
    selected_sample_ids: set[str],
    *,
    include_incomplete: bool,
) -> list[str]:
    models: list[str] = []
    for path in sorted(results_root.iterdir()):
        if not path.is_dir():
            continue
        if path.name.startswith("_") or path.name in DEFAULT_EXCLUDE:
            continue
        if path.name.startswith("evaluation_rerun_queues"):
            continue
        sample_ids = model_sample_ids(path)
        if not sample_ids:
            continue
        if include_incomplete or sample_ids == selected_sample_ids:
            models.append(path.name)
    return models


def evaluate_models(
    results_root: Path,
    models: list[str],
    *,
    line_tolerance: int,
    selected_sample_ids: set[str],
    selection_path: Path,
    include_incomplete_models: bool,
) -> dict[str, Any]:
    model_reports: dict[str, Any] = {}
    per_sample_rows: list[dict[str, Any]] = []
    for model in models:
        model_dir = results_root / model
        summary, rows = evaluate_model(
            model,
            model_dir,
            line_tolerance=line_tolerance,
            selected_sample_ids=selected_sample_ids,
        )
        model_reports[model] = summary
        per_sample_rows.extend(rows)
    return {
        "generated_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "results_root": str(results_root),
        "selection": str(selection_path),
        "selected_samples": len(selected_sample_ids),
        "line_tolerance": line_tolerance,
        "model_order": models,
        "models": model_reports,
        "per_sample": per_sample_rows,
        "notes": {
            "model_discovery": (
                "default discovery includes only result dirs whose sample IDs "
                "exactly match the selected sample set"
                if not include_incomplete_models
                else "incomplete result dirs were included by request"
            ),
            "reasoning_denominator": "quality-valid analysis artifacts only; counts are reported separately",
            "reachability_denominator": "samples with submitted deduplicated PoCs and evaluated reachability only",
            "no_poc_reachability": "samples without submitted PoCs are excluded from R1-R4/GT/FP denominators",
            "submission_metrics": "submission_rate and GT/FP outcomes are sample-level; avg_submission uses raw submission attempts among submitted samples",
            "context_denominator": "samples with ground_truth.json fine_trace and context_visit.json available",
        },
    }


def evaluate_model(
    model: str,
    model_dir: Path,
    *,
    line_tolerance: int,
    selected_sample_ids: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sample_dirs = sorted([
        path
        for path in model_dir.iterdir()
        if path.is_dir() and path.name in selected_sample_ids
    ])
    rows: list[dict[str, Any]] = []

    counts: Counter[str] = Counter()
    manifest_model_counts: Counter[str] = Counter()
    max_iter_counts: Counter[str] = Counter()

    trace_node_total = trace_node_covered = 0
    trace_edge_total = trace_edge_covered = 0
    trace_metric_values: dict[str, list[float]] = defaultdict(list)
    stage_hits: dict[str, int] = Counter()
    stage_totals: dict[str, int] = Counter()

    logic_values: dict[str, list[float]] = defaultdict(list)
    logic_macro_values: dict[str, list[float]] = defaultdict(list)

    context_file_total = context_file_covered = 0
    context_file_visited = context_file_relevant = 0
    context_function_file_visited = context_function_file_relevant = 0
    context_fn_total = context_fn_covered = 0
    context_fn_visited = context_fn_relevant = 0
    context_values: dict[str, list[float]] = defaultdict(list)

    reach_sample_denominator = 0
    reach_hits: Counter[str] = Counter()
    reach_candidates = Counter()

    for sample_dir in sample_dirs:
        sample_id = sample_dir.name
        counts["sample_dirs"] += 1
        row: dict[str, Any] = {
            "model": model,
            "sample_id": sample_id,
            "has_manifest": False,
            "manifest_model": "",
            "max_iter": "",
            "analysis_present": False,
            "analysis_structural_valid": False,
            "analysis_quality_valid": False,
            "analysis_error": "",
            "quality_error": "",
            "reasoning_scored": False,
            "submitted": False,
            "submitted_attempts": 0,
            "submitted_unique_pocs": 0,
            "reachability_present": False,
            "reachability_evaluated_unique_pocs": 0,
            "reachability_error": "",
            "context_scored": False,
            "context_error": "",
        }

        manifest, manifest_error = load_json(sample_dir / "manifest.json")
        if isinstance(manifest, dict):
            row["has_manifest"] = True
            counts["manifest_present"] += 1
            manifest_model = str(manifest.get("model") or "")
            max_iter = manifest.get("max_iter")
            row["manifest_model"] = manifest_model
            row["max_iter"] = "" if max_iter is None else str(max_iter)
            if manifest_model:
                manifest_model_counts[manifest_model] += 1
            if max_iter is not None:
                max_iter_counts[str(max_iter)] += 1
            submitted_attempts, submitted_unique = submission_counts_from_manifest(
                manifest
            )
            row["submitted_attempts"] = submitted_attempts
            row["submitted_unique_pocs"] = submitted_unique
            if submitted_attempts:
                row["submitted"] = True
                counts["submitted_samples"] += 1
                counts["submitted_attempts"] += submitted_attempts
                reach_candidates["submitted_unique_pocs"] += submitted_unique
        elif manifest_error:
            row["manifest_error"] = manifest_error

        analysis_path = sample_dir / "analysis.json"
        analysis_obj: dict[str, Any] | None = None
        if analysis_path.is_file():
            row["analysis_present"] = True
            counts["analysis_present"] += 1
            try:
                analysis_text = analysis_path.read_text(encoding="utf-8")
            except OSError as exc:
                row["analysis_error"] = f"read_error:{type(exc).__name__}:{exc}"
            else:
                structural_error = validate_analysis_artifact(analysis_text)
                if structural_error:
                    row["analysis_error"] = structural_error
                    counts["analysis_invalid"] += 1
                else:
                    row["analysis_structural_valid"] = True
                    counts["analysis_structural_valid"] += 1
                    analysis_obj = json.loads(analysis_text)
                    quality_error = validate_analysis_artifact_quality(analysis_text)
                    if quality_error:
                        row["quality_error"] = quality_error
                        counts["analysis_quality_invalid"] += 1
                    else:
                        row["analysis_quality_valid"] = True
                        counts["analysis_quality_valid"] += 1
        else:
            counts["analysis_missing"] += 1

        if row["analysis_quality_valid"] and analysis_obj is not None:
            score_reasoning(
                sample_id,
                analysis_obj,
                line_tolerance,
                row,
                counts,
                trace_metric_values,
                stage_hits,
                stage_totals,
                logic_values,
                logic_macro_values,
            )
            nodes = row.get("trace_nodes_total")
            node_hits = row.get("trace_nodes_covered")
            if isinstance(nodes, int) and isinstance(node_hits, int):
                trace_node_total += nodes
                trace_node_covered += node_hits
            edges = row.get("trace_edges_total")
            edge_hits = row.get("trace_edges_covered")
            if isinstance(edges, int) and isinstance(edge_hits, int):
                trace_edge_total += edges
                trace_edge_covered += edge_hits

        context = safe_score_context(sample_id, sample_dir)
        if context.get("unavailable"):
            row["context_error"] = str(context.get("unavailable") or "")
            counts["context_unavailable"] += 1
        else:
            row["context_scored"] = True
            counts["context_scored"] += 1
            row["context_visit_recoverable"] = bool(context.get("context_visit_recoverable"))
            row["context_visit_count"] = int(context.get("context_visit_count") or 0)
            if row["context_visit_recoverable"]:
                counts["context_visit_recoverable"] += 1
            if row["context_visit_count"]:
                counts["context_visit_nonempty"] += 1
            files = context.get("files") if isinstance(context.get("files"), dict) else {}
            functions = context.get("functions") if isinstance(context.get("functions"), dict) else {}
            function_files = (
                context.get("function_files")
                if isinstance(context.get("function_files"), dict)
                else {}
            )
            row["context_files_total"] = int(files.get("total") or 0)
            row["context_files_covered"] = int(files.get("covered") or 0)
            row["context_files_recall"] = files.get("recall")
            row["context_files_visited"] = int(files.get("visited") or 0)
            row["context_files_relevant"] = int(files.get("relevant") or 0)
            row["context_files_precision"] = files.get("precision")
            row["context_function_files_visited"] = int(function_files.get("visited") or 0)
            row["context_function_files_relevant"] = int(function_files.get("relevant") or 0)
            row["context_function_file_precision"] = function_files.get("precision")
            row["context_functions_total"] = int(functions.get("total") or 0)
            row["context_functions_covered"] = int(functions.get("covered") or 0)
            row["context_functions_recall"] = functions.get("recall")
            row["context_functions_visited"] = int(functions.get("visited") or 0)
            row["context_functions_relevant"] = int(functions.get("relevant") or 0)
            row["context_functions_precision"] = functions.get("precision")
            context_file_total += row["context_files_total"]
            context_file_covered += row["context_files_covered"]
            context_file_visited += row["context_files_visited"]
            context_file_relevant += row["context_files_relevant"]
            context_function_file_visited += row["context_function_files_visited"]
            context_function_file_relevant += row["context_function_files_relevant"]
            context_fn_total += row["context_functions_total"]
            context_fn_covered += row["context_functions_covered"]
            context_fn_visited += row["context_functions_visited"]
            context_fn_relevant += row["context_functions_relevant"]
            append_float(context_values, "file_recall_mean", row["context_files_recall"])
            append_float(context_values, "file_precision_mean", row["context_files_precision"])
            append_float(
                context_values,
                "function_file_precision_mean",
                row["context_function_file_precision"],
            )
            append_float(context_values, "function_recall_mean", row["context_functions_recall"])
            append_float(context_values, "function_precision_mean", row["context_functions_precision"])

        if row["submitted"]:
            score_reachability(sample_dir, row, counts, reach_candidates, reach_hits)
            if row.get("reachability_evaluated_unique_pocs", 0):
                reach_sample_denominator += 1

        rows.append(row)

    summary = {
        "model": model,
        "sample_dirs": counts["sample_dirs"],
        "manifest_present": counts["manifest_present"],
        "analysis_present": counts["analysis_present"],
        "analysis_missing": counts["analysis_missing"],
        "analysis_structural_valid": counts["analysis_structural_valid"],
        "analysis_quality_valid": counts["analysis_quality_valid"],
        "analysis_invalid": counts["analysis_invalid"],
        "analysis_quality_invalid": counts["analysis_quality_invalid"],
        "reasoning_scored": counts["reasoning_scored"],
        "reasoning_errors": counts["reasoning_errors"],
        "context_scored": counts["context_scored"],
        "context_unavailable": counts["context_unavailable"],
        "context_visit_recoverable": counts["context_visit_recoverable"],
        "context_visit_nonempty": counts["context_visit_nonempty"],
        "submitted_samples": counts["submitted_samples"],
        "submission_rate": ratio(counts["submitted_samples"], counts["sample_dirs"]),
        "avg_submission": ratio(
            counts["submitted_attempts"],
            counts["submitted_samples"],
        ),
        "submitted_attempts": counts["submitted_attempts"],
        "manifest_model_counts": dict(sorted(manifest_model_counts.items())),
        "max_iter_counts": dict(sorted(max_iter_counts.items(), key=sort_counter_key)),
    }
    summary.update({
        "trace_node_recall_weighted": ratio(trace_node_covered, trace_node_total),
        "trace_node_recall_mean": mean(trace_metric_values["trace_node_recall"]),
        "trace_nodes_total": trace_node_total,
        "trace_nodes_covered": trace_node_covered,
        "trace_edge_recall_weighted": ratio(trace_edge_covered, trace_edge_total),
        "trace_edge_recall_mean": mean(trace_metric_values["trace_edge_recall"]),
        "trace_edges_total": trace_edge_total,
        "trace_edges_covered": trace_edge_covered,
    })
    for key in STAGE_KEYS:
        summary[f"stage_{key}_coverage_rate"] = ratio(stage_hits[key], stage_totals[key])
        summary[f"stage_{key}_available"] = stage_totals[key]

    summary.update(logic_summary(logic_values, logic_macro_values))
    summary.update({
        "context_file_recall_weighted": ratio(context_file_covered, context_file_total),
        "context_file_recall_mean": mean(context_values["file_recall_mean"]),
        "context_files_total": context_file_total,
        "context_files_covered": context_file_covered,
        "context_file_precision_weighted": ratio(context_file_relevant, context_file_visited),
        "context_file_precision_mean": mean(context_values["file_precision_mean"]),
        "context_files_visited": context_file_visited,
        "context_files_relevant": context_file_relevant,
        "context_function_file_precision_weighted": ratio(
            context_function_file_relevant,
            context_function_file_visited,
        ),
        "context_function_file_precision_mean": mean(
            context_values["function_file_precision_mean"]
        ),
        "context_function_files_visited": context_function_file_visited,
        "context_function_files_relevant": context_function_file_relevant,
        "context_function_recall_weighted": ratio(context_fn_covered, context_fn_total),
        "context_function_recall_mean": mean(context_values["function_recall_mean"]),
        "context_functions_total": context_fn_total,
        "context_functions_covered": context_fn_covered,
        "context_function_precision_weighted": ratio(context_fn_relevant, context_fn_visited),
        "context_function_precision_mean": mean(context_values["function_precision_mean"]),
        "context_functions_visited": context_fn_visited,
        "context_functions_relevant": context_fn_relevant,
    })
    summary.update(reachability_summary(counts, reach_candidates, reach_hits, reach_sample_denominator))
    return summary, rows


def score_reasoning(
    sample_id: str,
    analysis_obj: dict[str, Any],
    line_tolerance: int,
    row: dict[str, Any],
    counts: Counter[str],
    trace_metric_values: dict[str, list[float]],
    stage_hits: dict[str, int],
    stage_totals: dict[str, int],
    logic_values: dict[str, list[float]],
    logic_macro_values: dict[str, list[float]],
) -> None:
    try:
        trace = score_fine_trace_coverage(
            sample_id,
            analysis_obj,
            line_tolerance=line_tolerance,
        )
        logic = score_vuln_logic(
            sample_id,
            analysis_obj.get("vuln_logic") or {},
            line_tolerance=line_tolerance,
        )
    except Exception as exc:  # noqa: BLE001 - keep aggregation moving.
        row["reasoning_error"] = f"{type(exc).__name__}:{exc}"
        counts["reasoning_errors"] += 1
        return

    row["reasoning_scored"] = True
    counts["reasoning_scored"] += 1

    nodes = trace.get("nodes") if isinstance(trace.get("nodes"), dict) else {}
    edges = trace.get("edges") if isinstance(trace.get("edges"), dict) else {}
    row["trace_nodes_total"] = int(nodes.get("total") or 0)
    row["trace_nodes_covered"] = int(nodes.get("covered") or 0)
    row["trace_node_recall"] = nodes.get("recall")
    row["trace_edges_total"] = int(edges.get("total") or 0)
    row["trace_edges_covered"] = int(edges.get("covered") or 0)
    row["trace_edge_recall"] = edges.get("recall")
    append_float(trace_metric_values, "trace_node_recall", row["trace_node_recall"])
    append_float(trace_metric_values, "trace_edge_recall", row["trace_edge_recall"])

    stages = trace.get("stage_coverage") if isinstance(trace.get("stage_coverage"), dict) else {}
    for key in STAGE_KEYS:
        value = stages.get(key)
        row[f"stage_{key}"] = value
        if value is not None:
            stage_totals[key] += 1
            if value is True:
                stage_hits[key] += 1

    for key in LOGIC_SCALAR_KEYS:
        value = logic.get(key)
        row[key] = value
        append_float(logic_values, key, value)

    # Human-facing names matching the evaluation rubric.
    row["source_location"] = logic.get("source_location_score")
    row["source_operand"] = logic.get("source_operand_score")
    row["root_cause_location"] = logic.get("root_cause_location_score")
    row["root_cause_operand"] = logic.get("root_cause_operand_score")
    row["root_cause_relation"] = logic.get("root_cause_relation_score")
    row["sink_location"] = logic.get("sink_location_score")
    row["sink_operand"] = logic.get("sink_operand_score")
    row["sink_relation"] = logic.get("sink_relation_score")
    row["propagation_node_recall"] = logic.get("propagation_node_recall_score")
    row["propagation_node_precision"] = logic.get("propagation_node_precision_score")
    row["propagation_ordered_coverage"] = logic.get("propagation_ordered_coverage_score")

    append_float(logic_macro_values, "location_macro", macro([
        row["source_location"],
        row["root_cause_location"],
        row["sink_location"],
        row["propagation_node_recall"],
    ]))
    append_float(logic_macro_values, "operand_macro", macro([
        row["source_operand"],
        row["root_cause_operand"],
        row["sink_operand"],
        row["propagation_node_precision"],
    ]))
    append_float(logic_macro_values, "relation_macro", macro([
        row["root_cause_relation"],
        row["sink_relation"],
        row["propagation_ordered_coverage"],
    ]))


def score_reachability(
    sample_dir: Path,
    row: dict[str, Any],
    counts: Counter[str],
    reach_candidates: Counter[str],
    reach_hits: Counter[str],
) -> None:
    reach_path = sample_dir / "reachability_eval.json"
    if not reach_path.is_file():
        counts["reachability_missing_for_submitted"] += 1
        row["reachability_error"] = "missing reachability_eval.json for submitted PoC"
        return
    reach, error = load_json(reach_path)
    if not isinstance(reach, dict):
        counts["reachability_invalid_for_submitted"] += 1
        row["reachability_error"] = error or "invalid reachability_eval.json"
        return

    row["reachability_present"] = True
    counts["reachability_present_for_submitted"] += 1
    summary = reach.get("summary") if isinstance(reach.get("summary"), dict) else {}
    candidates = reach.get("candidates") if isinstance(reach.get("candidates"), list) else []
    evaluated = int(summary.get("evaluated_unique_pocs") or 0)
    row["reachability_evaluated_unique_pocs"] = evaluated
    reach_candidates["evaluated_unique_pocs"] += evaluated
    reach_candidates["candidate_errors"] += int(summary.get("candidate_errors") or 0)
    gt_triggered_sample = any(
        not candidate.get("error")
        and candidate.get("target_vulnerability_triggered") is True
        for candidate in candidates
    )
    false_positive_sample = any(
        not candidate.get("error")
        and candidate_is_false_positive(candidate)
        for candidate in candidates
    )
    if not candidates:
        gt_triggered_sample = bool(summary.get("any_target_vulnerability_triggered"))
        false_positive_sample = int(
            summary.get("nonzero_exit_false_positives") or 0
        ) > 0
    row["reach_GT_triggered_sample"] = gt_triggered_sample
    row["reach_FP_sample"] = false_positive_sample
    if gt_triggered_sample:
        reach_candidates["gt_triggered_samples"] += 1
    if false_positive_sample:
        reach_candidates["false_positive_samples"] += 1
    if not evaluated:
        counts["reachability_no_evaluated_candidates_for_submitted"] += 1
        return

    any_reached = summary.get("any_reached") if isinstance(summary.get("any_reached"), dict) else {}
    for key in REACH_KEYS:
        value = bool(any_reached.get(key))
        row[f"reach_{key}"] = value
        if value:
            reach_hits[key] += 1
    gt_hit = bool(summary.get("any_target_vulnerability_triggered"))
    fp_hit = false_positive_sample
    row["reach_GT_triggered"] = gt_hit
    row["reach_FP_nonzero_exit"] = fp_hit
    if gt_hit:
        reach_hits["GT_triggered_samples"] += 1
    if fp_hit:
        reach_hits["FP_nonzero_exit_samples"] += 1


def safe_score_context(sample_id: str, sample_dir: Path) -> dict[str, Any]:
    try:
        return score_context_recall(sample_id, sample_dir)
    except Exception as exc:  # noqa: BLE001 - keep aggregation moving.
        return {
            "unavailable": f"{type(exc).__name__}:{exc}",
            "files": {"total": 0, "covered": 0, "recall": None},
            "functions": {"total": 0, "covered": 0, "recall": None},
        }


def logic_summary(
    logic_values: dict[str, list[float]],
    logic_macro_values: dict[str, list[float]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "logic_location_macro_mean": mean(logic_macro_values["location_macro"]),
        "logic_operand_macro_mean": mean(logic_macro_values["operand_macro"]),
        "logic_relation_macro_mean": mean(logic_macro_values["relation_macro"]),
    }
    aliases = {
        "source_location_score": "source_location_mean",
        "source_operand_score": "source_operand_mean",
        "root_cause_location_score": "root_cause_location_mean",
        "root_cause_operand_score": "root_cause_operand_mean",
        "root_cause_relation_score": "root_cause_relation_mean",
        "sink_location_score": "sink_location_mean",
        "sink_operand_score": "sink_operand_mean",
        "sink_relation_score": "sink_relation_mean",
        "propagation_node_recall_score": "propagation_node_recall_mean",
        "propagation_node_precision_score": "propagation_node_precision_mean",
        "propagation_ordered_coverage_score": "propagation_ordered_coverage_mean",
    }
    for key, alias in aliases.items():
        result[alias] = mean(logic_values[key])
    return result


def reachability_summary(
    counts: Counter[str],
    reach_candidates: Counter[str],
    reach_hits: Counter[str],
    reach_sample_denominator: int,
) -> dict[str, Any]:
    submitted = counts["submitted_samples"]
    evaluated_candidates = reach_candidates["evaluated_unique_pocs"]
    result: dict[str, Any] = {
        "reachability_submitted_samples": submitted,
        "reachability_submitted_attempts": counts["submitted_attempts"],
        "reachability_present_for_submitted": counts["reachability_present_for_submitted"],
        "reachability_missing_for_submitted": counts["reachability_missing_for_submitted"],
        "reachability_invalid_for_submitted": counts["reachability_invalid_for_submitted"],
        "reachability_no_evaluated_candidates_for_submitted": counts["reachability_no_evaluated_candidates_for_submitted"],
        "reachability_evaluated_submitted_samples": reach_sample_denominator,
        "reachability_evaluated_submitted_sample_rate": ratio(reach_sample_denominator, submitted),
        "reachability_submitted_unique_pocs": reach_candidates["submitted_unique_pocs"],
        "reachability_evaluated_unique_pocs": evaluated_candidates,
        "reachability_candidate_errors": reach_candidates["candidate_errors"],
        "gt_triggered_samples": reach_candidates["gt_triggered_samples"],
        "false_positive_samples": reach_candidates["false_positive_samples"],
        "gt_trigger_sample_rate": ratio(
            reach_candidates["gt_triggered_samples"],
            submitted,
        ),
        "false_positive_sample_rate": ratio(
            reach_candidates["false_positive_samples"],
            submitted,
        ),
        "reachability_GT_trigger_sample_rate": ratio(
            reach_hits["GT_triggered_samples"],
            reach_sample_denominator,
        ),
        "reachability_FP_nonzero_exit_sample_rate": ratio(
            reach_hits["FP_nonzero_exit_samples"],
            reach_sample_denominator,
        ),
    }
    for key in REACH_KEYS:
        result[f"reachability_{key}_sample_rate"] = ratio(
            reach_hits[key],
            reach_sample_denominator,
        )
        result[f"reachability_{key}_sample_hits"] = reach_hits[key]
    result["reachability_GT_trigger_sample_hits"] = reach_hits["GT_triggered_samples"]
    result["reachability_FP_nonzero_exit_sample_hits"] = reach_hits["FP_nonzero_exit_samples"]
    return result


def write_reports(report: dict[str, Any], prefix: Path) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_summary_csv(report, prefix.with_suffix(".summary.csv"))
    write_per_sample_csv(report, prefix.with_suffix(".per_sample.csv"))


def write_summary_csv(report: dict[str, Any], path: Path) -> None:
    models = report["models"]
    rows = [flatten_summary(value) for value in models.values()]
    fieldnames = sorted({key for row in rows for key in row})
    preferred = [
        "model",
        "sample_dirs",
        "analysis_present",
        "analysis_quality_valid",
        "reasoning_scored",
        "context_scored",
        "submitted_samples",
        "submission_rate",
        "avg_submission",
        "submitted_attempts",
        "gt_triggered_samples",
        "false_positive_samples",
        "reachability_evaluated_submitted_samples",
        "reachability_missing_for_submitted",
        "reachability_R1_input_admitted_sample_rate",
        "reachability_R2_source_reached_sample_rate",
        "reachability_R3_root_cause_reached_sample_rate",
        "reachability_R4_sink_reached_sample_rate",
        "reachability_GT_trigger_sample_rate",
        "reachability_FP_nonzero_exit_sample_rate",
        "context_file_recall_weighted",
        "context_file_precision_weighted",
        "context_function_file_precision_weighted",
        "context_function_recall_weighted",
        "context_function_precision_weighted",
        "trace_node_recall_weighted",
        "trace_edge_recall_weighted",
        "logic_location_macro_mean",
        "logic_operand_macro_mean",
        "logic_relation_macro_mean",
        "source_location_mean",
        "source_operand_mean",
        "root_cause_location_mean",
        "root_cause_operand_mean",
        "root_cause_relation_mean",
        "sink_location_mean",
        "sink_operand_mean",
        "sink_relation_mean",
        "propagation_node_recall_mean",
        "propagation_node_precision_mean",
        "propagation_ordered_coverage_mean",
        "manifest_model_counts",
        "max_iter_counts",
    ]
    ordered = preferred + [key for key in fieldnames if key not in preferred]
    write_csv(path, rows, ordered)


def write_per_sample_csv(report: dict[str, Any], path: Path) -> None:
    rows = report["per_sample"]
    preferred = [
        "model",
        "sample_id",
        "manifest_model",
        "max_iter",
        "analysis_present",
        "analysis_quality_valid",
        "reasoning_scored",
        "submitted",
        "submitted_attempts",
        "submitted_unique_pocs",
        "reachability_present",
        "reachability_evaluated_unique_pocs",
        "reach_R1_input_admitted",
        "reach_R2_source_reached",
        "reach_R3_root_cause_reached",
        "reach_R4_sink_reached",
        "reach_GT_triggered",
        "reach_FP_nonzero_exit",
        "reach_GT_triggered_sample",
        "reach_FP_sample",
        "context_files_recall",
        "context_files_precision",
        "context_function_file_precision",
        "context_functions_recall",
        "context_functions_precision",
        "context_visit_recoverable",
        "context_visit_count",
        "trace_node_recall",
        "trace_edge_recall",
        "source_location",
        "source_operand",
        "root_cause_location",
        "root_cause_operand",
        "root_cause_relation",
        "sink_location",
        "sink_operand",
        "sink_relation",
        "propagation_node_recall",
        "propagation_node_precision",
        "propagation_ordered_coverage",
        "analysis_error",
        "quality_error",
        "reasoning_error",
        "reachability_error",
        "context_error",
    ]
    fieldnames = sorted({key for row in rows for key in row})
    ordered = preferred + [key for key in fieldnames if key not in preferred]
    write_csv(path, rows, ordered)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})


def flatten_summary(summary: dict[str, Any]) -> dict[str, Any]:
    row = dict(summary)
    for key in ("manifest_model_counts", "max_iter_counts"):
        row[key] = json.dumps(row.get(key) or {}, ensure_ascii=False, sort_keys=True)
    return row


def load_json(path: Path) -> tuple[Any | None, str | None]:
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}:{exc}"


def submission_counts_from_manifest(manifest: dict[str, Any]) -> tuple[int, int]:
    deduplicated = manifest.get("deduplicated_pocs")
    if isinstance(deduplicated, list):
        unique_count = len(deduplicated)
    else:
        unique_count = nonnegative_int(
            (manifest.get("poc_deduplication") or {}).get(
                "deduplicated_poc_count"
            )
        )

    attempts_count = nonnegative_int(manifest.get("num_submission_attempts"))
    dedup = manifest.get("poc_deduplication") or {}
    if attempts_count == 0:
        attempts_count = nonnegative_int(dedup.get("total_poc_submissions"))
    if attempts_count == 0:
        attempts_count = nonnegative_int(dedup.get("input_attempts"))
    attempts = manifest.get("submission_attempts")
    if attempts_count == 0 and isinstance(attempts, list):
        attempts_count = len(attempts)
    if attempts_count == 0:
        attempts_count = unique_count
    return attempts_count, unique_count


def candidate_is_false_positive(candidate: dict[str, Any]) -> bool:
    return (
        false_positive_exit(candidate.get("original_exit_code"))
        and candidate.get("target_vulnerability_triggered") is not True
    )


def false_positive_exit(value: Any) -> bool:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return False
    return code not in (0, 300)


def append_float(store: dict[str, list[float]], key: str, value: Any) -> None:
    number = as_float(value)
    if number is not None:
        store[key].append(number)


def macro(values: list[Any]) -> float | None:
    numbers = [value for value in (as_float(item) for item in values) if value is not None]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return float(numerator) / float(denominator)


def nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def sort_counter_key(item: tuple[str, int]) -> tuple[int, str]:
    key, _ = item
    try:
        return (0, f"{int(key):012d}")
    except ValueError:
        return (1, key)


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, float):
        return f"{value:.10g}"
    if value is None:
        return ""
    return value


if __name__ == "__main__":
    raise SystemExit(main())
