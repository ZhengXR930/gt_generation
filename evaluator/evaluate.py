#!/usr/bin/env python3
"""Unified non-LLM evaluation over frozen verified invariant graphs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evaluator.compiled_graph import compile_invariant_graph
from evaluator.reasoning.analysis_artifact import (
    parse_analysis_artifact,
    validate_analysis_artifact,
    validate_analysis_artifact_quality,
)
from evaluator.reasoning.vuln_logic_scoring import score_vuln_logic
from evaluator.reasoning.fine_trace_coverage import score_fine_trace_coverage
from evaluator.reasoning.context_recall import score_context_recall
from evaluator.reachability.core import evaluate_r1_r5
from evaluator.reachability.probes import compile_runtime_probes

REPO_ROOT = Path(__file__).resolve().parents[1]
GT_RESULTS = REPO_ROOT / "gt_results"
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"
_RUNTIME_TRACE_EDGE_BYTES = 256 * 1024


def _read_runtime_trace_for_oracle(path: Path) -> str | None:
    """Read enough runtime output for sanitizer oracle parsing without hanging.

    Some failed PoCs print very large logs.  The sanitizer signature and stack
    are normally near the beginning or end, so keep both edges and avoid feeding
    multi-megabyte non-sanitizer text into regex parsing.
    """
    if not path.is_file():
        return None
    size = path.stat().st_size
    edge = _RUNTIME_TRACE_EDGE_BYTES
    with path.open("rb") as handle:
        if size <= edge * 2:
            data = handle.read()
        else:
            head = handle.read(edge)
            handle.seek(max(size - edge, 0))
            tail = handle.read(edge)
            data = (
                head
                + b"\n\n[... runtime log truncated for evaluation ...]\n\n"
                + tail
            )
    return data.decode("utf-8", errors="replace")


def audit_gt() -> dict[str, Any]:
    rows = []
    for gt_dir in sorted(GT_RESULTS.iterdir()):
        if not (gt_dir / "verified_assertions.json").is_file():
            continue
        try:
            compiled = compile_invariant_graph(gt_dir)
            graph = compiled.runtime
            probes, probe_errors = compile_runtime_probes(graph)
            rows.append({
                "sample_id": gt_dir.name,
                "conditions": len(graph.conditions),
                "root_conditions": len(graph.root_conditions),
                "propagation_conditions": len(graph.propagation_conditions),
                "observed_conditions": len(graph.observed_conditions),
                "reasoning_nodes": len(compiled.nodes),
                "reasoning_edges": len(compiled.edges),
                "admission": {
                    "file": compiled.admission.file,
                    "function": compiled.admission.function,
                    "line": compiled.admission.line,
                },
                "errors": list(compiled.errors),
                "runtime_probe_events": len(probes),
                "runtime_probe_contract_compiled": bool(probes and not probe_errors),
                "runtime_probe_errors": probe_errors,
            })
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            rows.append({
                "sample_id": gt_dir.name,
                "conditions": 0,
                "errors": [f"{type(exc).__name__}: {exc}"],
            })
    return {
        "evaluation_protocol": "compiled-invariant-graph-audit-v2",
        "samples": len(rows),
        "conditions": sum(item["conditions"] for item in rows),
        "samples_with_errors": sum(bool(item["errors"]) for item in rows),
        "runtime_probe_contract_compiled_samples": sum(
            bool(item.get("runtime_probe_contract_compiled")) for item in rows
        ),
        "rows": rows,
    }


def evaluate_sample(
    model: str,
    sample_dir: Path,
    *,
    require_analysis_quality: bool = True,
) -> dict[str, Any]:
    sample_id = sample_dir.name
    gt_dir = GT_RESULTS / sample_id
    result: dict[str, Any] = {"model": model, "sample_id": sample_id}
    analysis_artifact, analysis_load_error = _load_analysis_artifact(sample_dir)
    try:
        result["fine_trace_coverage"] = score_fine_trace_coverage(
            sample_id,
            analysis_artifact,
            gt_dir=gt_dir,
        )
        if analysis_load_error:
            result["fine_trace_coverage"]["analysis_load_error"] = analysis_load_error
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        result["fine_trace_coverage"] = {
            "unavailable": f"fine_trace coverage failed: {type(exc).__name__}: {exc}"
        }
    try:
        result["context_recall"] = score_context_recall(
            sample_id,
            sample_dir,
            gt_dir=gt_dir,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        result["context_recall"] = {
            "unavailable": f"context recall failed: {type(exc).__name__}: {exc}"
        }

    logic, quality_error = _load_vuln_logic(
        sample_dir,
        require_quality=require_analysis_quality,
    )
    if logic is not None:
        try:
            result["reasoning"] = score_vuln_logic(
                sample_id,
                logic,
                gt_dir=gt_dir,
            )
            if quality_error:
                result["reasoning"]["analysis_quality_error"] = quality_error
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            result["reasoning"] = {
                "unavailable": f"vuln_logic scoring failed: {type(exc).__name__}: {exc}"
            }
    else:
        result["reasoning"] = {"unavailable": "analysis.json missing or invalid"}

    submitted_unique_pocs = 0
    manifest_path = sample_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        deduplicated = manifest.get("deduplicated_pocs")
        if isinstance(deduplicated, list):
            submitted_unique_pocs = len(deduplicated)
        else:
            submitted_unique_pocs = int(
                ((manifest.get("poc_deduplication") or {}).get(
                    "deduplicated_poc_count"
                ))
                or 0
            )

    old_reach_path = sample_dir / "reachability_eval.json"
    if not old_reach_path.is_file():
        result["runtime"] = {
            "evaluation_protocol": "location-reachability-batch-v3",
            "submitted_unique_pocs": submitted_unique_pocs,
            "reachability_executed_candidates": 0,
            "candidates": [],
            "unavailable": (
                "not applicable: no submitted PoC"
                if submitted_unique_pocs == 0
                else "reachability has not been executed"
            ),
        }
        return result
    old_reach = json.loads(old_reach_path.read_text(encoding="utf-8"))
    gt_path = gt_dir / "ground_truth.json"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    candidates = []
    for item in old_reach.get("candidates", []):
        attempt_id = str(item.get("attempt_id") or "")
        runtime_relative = str(item.get("runtime_output_path") or "")
        runtime_path = sample_dir / runtime_relative
        hit_path = (
            sample_dir / "reachability" / attempt_id / "reachability_hits.json"
        )
        hit_source = None
        location_report = None
        if hit_path.is_file():
            ledger_error = _saved_gdb_ledger_error(hit_path.parent)
            if ledger_error is None:
                loaded = json.loads(hit_path.read_text(encoding="utf-8"))
                hits = loaded.get("hits", []) if isinstance(loaded, dict) else []
                runtime_text = _read_runtime_trace_for_oracle(runtime_path)
                location_report = evaluate_r1_r5(
                    gt=gt,
                    hits=hits,
                    sanitizer_trace=runtime_text,
                )
                hit_source = str(hit_path.relative_to(sample_dir))
        else:
            ledger_error = "saved location hit ledger is missing"
        candidate = {
            "attempt_id": attempt_id,
            "sequence_in_run": item.get("sequence_in_run"),
            "execution_status": item.get("execution_status"),
            "execution_error": item.get("error"),
            "location_hit_source": hit_source,
            "target_vulnerability_triggered": item.get(
                "target_vulnerability_triggered"
            ),
        }
        if location_report is not None:
            candidate["execution_status"] = "executed"
            if location_report.get("target_vulnerability_triggered") is None:
                location_report["target_vulnerability_triggered"] = item.get(
                    "target_vulnerability_triggered"
                )
            candidate["location_reachability"] = location_report
            candidate["target_vulnerability_triggered"] = location_report.get(
                "target_vulnerability_triggered"
            )
        else:
            candidate["execution_status"] = (
                candidate.get("execution_status") or "infrastructure_unavailable"
            )
            candidate["location_reachability"] = {
                "evaluation_protocol": "location-reachability-v3",
                "R1_input_admitted": None,
                "R2_source_reached": None,
                "R3_root_cause_reached": None,
                "R4_sink_reached": None,
                "R5_sanitizer_triggered": None,
                "target_vulnerability_triggered": None,
                "reachability_depth": None,
                "unavailable": (
                    f"{ledger_error}; old combined R3/R4 fields cannot be "
                    "safely reinterpreted"
                ),
            }
        candidates.append(candidate)
    result["runtime"] = {
        "evaluation_protocol": "location-reachability-batch-v3",
        "submitted_unique_pocs": submitted_unique_pocs,
        "reachability_executed_candidates": len(candidates),
        "candidates": candidates,
        "location_evaluated_candidates": sum(
            item["location_reachability"].get("reachability_checked") is True
            for item in candidates
        ),
    }
    return result


def _load_analysis_artifact(sample_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    analysis_path = sample_dir / "analysis.json"
    if not analysis_path.is_file():
        return None, "analysis.json missing"
    try:
        value = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"analysis.json invalid: {type(exc).__name__}: {exc}"
    if not isinstance(value, dict):
        return None, "analysis.json is not an object"
    return value, None


def _load_vuln_logic(
    sample_dir: Path,
    *,
    require_quality: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    analysis_path = sample_dir / "analysis.json"
    if analysis_path.is_file():
        text = analysis_path.read_text(encoding="utf-8")
        quality_error = validate_analysis_artifact_quality(text)
        if require_quality and quality_error is not None:
            return None, quality_error
        if quality_error is not None:
            structure_error = validate_analysis_artifact(text)
            if structure_error is not None:
                return None, structure_error
        value = parse_analysis_artifact(text)
        logic = value.get("vuln_logic") if value is not None else None
        return (logic, quality_error) if isinstance(logic, dict) else (None, quality_error)
    return None, None


def _saved_gdb_ledger_error(output_dir: Path) -> str | None:
    """Reject ledgers produced by known-invalid historical invocations."""
    command_path = output_dir / "gdb_command.json"
    if not command_path.is_file():
        return "saved GDB command metadata is missing"
    try:
        command = json.loads(command_path.read_text(encoding="utf-8")).get(
            "command", []
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return f"saved GDB command metadata is invalid: {type(exc).__name__}"
    # Do not reject "-runs=0" by itself.  libFuzzer-style ARVO targets commonly
    # use "-runs=0 <input-file>" and still execute the submitted PoC once; when
    # no breakpoint is hit, an empty saved ledger is a valid R0 observation.
    stderr_path = output_dir / "gdb_stderr.txt"
    if stderr_path.is_file() and "error while loading shared libraries" in (
        stderr_path.read_text(encoding="utf-8", errors="replace")
    ):
        return "GDB inferior could not load a required shared library"
    return None


def evaluate_batch(
    models: list[str],
    samples: set[str],
    *,
    require_analysis_quality: bool = True,
) -> dict[str, Any]:
    rows = []
    for model in models:
        model_dir = POC_RESULTS / model
        for sample_dir in sorted(path for path in model_dir.iterdir() if path.is_dir()):
            if samples and sample_dir.name not in samples:
                continue
            if not (GT_RESULTS / sample_dir.name / "verified_assertions.json").is_file():
                continue
            rows.append(
                evaluate_sample(
                    model,
                    sample_dir,
                    require_analysis_quality=require_analysis_quality,
                )
            )
    return {
        "evaluation_protocol": "unified-invariant-location-evaluation-v3",
        "models": models,
        "rows": rows,
        "summary": {
            model: _summarize(
                [item for item in rows if item.get("model") == model]
            )
            for model in models
        },
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reasoning = [
        item["reasoning"] for item in rows
        if item.get("reasoning", {}).get("evaluation_protocol")
        == "vuln-logic-invariant-reasoning-v2"
    ]
    fine_trace = [
        item["fine_trace_coverage"] for item in rows
        if item.get("fine_trace_coverage", {}).get("evaluation_protocol")
        == "fine-trace-coverage-v2"
    ]
    context_recall = [
        item["context_recall"] for item in rows
        if item.get("context_recall", {}).get("evaluation_protocol")
        == "context-function-recall-v1"
        and not item.get("context_recall", {}).get("unavailable")
    ]
    context_recoverable = [
        item for item in context_recall
        if item.get("context_visit_recoverable") is True
    ]
    context_with_functions = [
        item for item in context_recoverable
        if int(item.get("visit_functions_total") or 0) > 0
    ]
    candidates = [
        candidate
        for row in rows
        for candidate in (row.get("runtime") or {}).get("candidates", [])
    ]
    locations = [item["location_reachability"] for item in candidates]
    evaluated_locations = [
        item for item in locations if item.get("reachability_checked") is True
    ]
    recorded_unavailable_locations = [
        item for item in locations if item.get("reachability_checked") is not True
    ]
    submitted = sum(
        int((row.get("runtime") or {}).get("submitted_unique_pocs") or 0)
        for row in rows
    )
    r1_known = [
        item for item in locations
        if item.get("R1_input_admitted") is not None
    ]
    r2_known = [
        item for item in locations
        if item.get("R2_source_reached") is not None
    ]
    r2_reached = [item for item in locations if item.get("R2_source_reached") is True]
    r3_known = [
        item for item in locations
        if item.get("R3_root_cause_reached") is not None
    ]
    r4_known = [
        item for item in locations if item.get("R4_sink_reached") is not None
    ]
    trigger_known = [
        candidate for candidate in candidates
        if candidate.get("target_vulnerability_triggered") is not None
    ]
    r5_known = [
        item for item in locations if item.get("R5_sanitizer_triggered") is not None
    ]
    propagation_chain_total = _sum_int(reasoning, "propagation_total")
    propagation_chain_loc_hits = _sum_int(reasoning, "propagation_loc_hits")
    propagation_chain_partial_hits = _sum_int(reasoning, "propagation_partial_hits")
    propagation_chain_carrier_hits = _sum_int(
        reasoning, "propagation_carrier_hits"
    )
    propagation_chain_carrier_partial_hits = _sum_int(
        reasoning, "propagation_carrier_partial_hits"
    )
    propagation_chain_exact_full_hits = _sum_int(
        reasoning, "propagation_exact_full_hits"
    )
    propagation_chain_full_hits = _sum_int(reasoning, "propagation_full_hits")
    propagation_chain_names = _propagation_chain_names(reasoning)
    depth_distribution = {
        depth: sum(item.get("reachability_depth") == depth for item in locations)
        for depth in ("R0", "R1", "R2", "R3", "R4", "R5")
    }
    depth_distribution["unknown"] = sum(
        item.get("reachability_depth") in (None, "not_checked") for item in locations
    )
    return {
        "samples": len(rows),
        "samples_with_submitted_poc": sum(
            int((row.get("runtime") or {}).get("submitted_unique_pocs") or 0) > 0
            for row in rows
        ),
        "samples_without_poc_not_applicable": sum(
            int((row.get("runtime") or {}).get("submitted_unique_pocs") or 0) == 0
            for row in rows
        ),
        "samples_with_reachability_execution": sum(
            int((row.get("runtime") or {}).get("location_evaluated_candidates") or 0) > 0
            for row in rows
        ),
        "reasoning_scored_samples": len(reasoning),
        "fine_trace_scored_samples": len(fine_trace),
        "fine_trace_node_recall": _mean_nested(fine_trace, "nodes", "recall"),
        "fine_trace_edge_recall": _mean_nested(fine_trace, "edges", "recall"),
        "context_recall_scored_samples": len(context_recoverable),
        "context_recall_files_present_samples": len(context_recall),
        "context_function_scored_samples": len(context_with_functions),
        "context_file_recall": _mean_nested(context_recoverable, "files", "recall"),
        "context_function_recall": _mean_nested(context_with_functions, "functions", "recall"),
        "fine_trace_stage_coverage": {
            stage: _mean_stage(fine_trace, stage)
            for stage in ("parser", "source", "root_cause", "sink", "trigger")
        },
        "reasoning_dimensions": {
            "source": {
                "loc": _mean(reasoning, "source_loc_score"),
                "partial": None,
                "full": _mean(reasoning, "source_full_score"),
            },
            "propagation": {
                "loc": _mean(reasoning, "propagation_loc_score"),
                "partial": _mean(reasoning, "propagation_partial_score"),
                "full": _mean(reasoning, "propagation_full_score"),
            },
            "obligation": {
                "loc": _mean(reasoning, "obligation_loc_score"),
                "partial": _mean(reasoning, "obligation_partial_score"),
                "full": _mean(reasoning, "obligation_full_score"),
            },
            "sink": {
                "loc": _mean(reasoning, "sink_loc_score"),
                "partial": _mean(reasoning, "sink_partial_score"),
                "full": _mean(reasoning, "sink_full_score"),
            },
        },
        "propagation_gap": _mean(reasoning, "propagation_gap"),
        "propagation_chain_total": propagation_chain_total,
        "propagation_chain_loc_hits": propagation_chain_loc_hits,
        "propagation_chain_loc_rate": _ratio(
            propagation_chain_loc_hits, propagation_chain_total
        ),
        "propagation_chain_partial_hits": propagation_chain_partial_hits,
        "propagation_chain_partial_rate": _ratio(
            propagation_chain_partial_hits, propagation_chain_total
        ),
        "propagation_chain_carrier_hits": propagation_chain_carrier_hits,
        "propagation_chain_carrier_rate": _ratio(
            propagation_chain_carrier_hits, propagation_chain_total
        ),
        "propagation_chain_carrier_partial_hits": (
            propagation_chain_carrier_partial_hits
        ),
        "propagation_chain_carrier_partial_rate": _ratio(
            propagation_chain_carrier_partial_hits, propagation_chain_total
        ),
        "propagation_chain_exact_full_hits": propagation_chain_exact_full_hits,
        "propagation_chain_exact_full_rate": _ratio(
            propagation_chain_exact_full_hits, propagation_chain_total
        ),
        "propagation_chain_full_hits": propagation_chain_full_hits,
        "propagation_chain_full_rate": _ratio(
            propagation_chain_full_hits, propagation_chain_total
        ),
        "propagation_chain_names": propagation_chain_names,
        "norm_unresolved_rate": _mean(reasoning, "norm_unresolved_rate"),
        "norm_unresolved_rate_all": _mean(reasoning, "norm_unresolved_rate_all"),
        "submitted_unique_pocs": submitted,
        "reachability_candidate_records": len(candidates),
        "reachability_executed_candidates": len(evaluated_locations),
        # Do not conflate a candidate that has never been scheduled with an
        # execution whose runtime or observation infrastructure was unavailable.
        "reachability_not_scheduled_candidates": max(
            submitted - len(candidates), 0
        ),
        "reachability_infrastructure_unavailable_candidates": len(
            recorded_unavailable_locations
        ),
        # Backward-compatible alias, now restricted to recorded executions.
        "reachability_unavailable_candidates": len(recorded_unavailable_locations),
        "reachability_execution_coverage": (
            len(evaluated_locations) / submitted if submitted else None
        ),
        "reachability_depth_distribution": depth_distribution,
        "R1_evaluable_candidates": len(r1_known),
        "R1_reached_candidates": sum(
            item.get("R1_input_admitted") is True for item in r1_known
        ),
        "R1_reach_rate": _mean(r1_known, "R1_input_admitted"),
        "R2_evaluable_candidates": len(r2_known),
        "R2_reached_candidates": len(r2_reached),
        "R2_reach_rate": _mean(r2_known, "R2_source_reached"),
        "R3_evaluable_candidates": len(r3_known),
        "R3_reached_candidates": sum(
            item.get("R3_root_cause_reached") is True for item in r3_known
        ),
        "R3_reach_rate": _mean(r3_known, "R3_root_cause_reached"),
        "R4_evaluable_candidates": len(r4_known),
        "R4_reached_candidates": sum(
            item.get("R4_sink_reached") is True for item in r4_known
        ),
        "R4_reach_rate": _mean(r4_known, "R4_sink_reached"),
        "R5_evaluable_candidates": len(r5_known),
        "R5_reached_candidates": sum(
            item.get("R5_sanitizer_triggered") is True for item in r5_known
        ),
        "R5_reach_rate": _mean(r5_known, "R5_sanitizer_triggered"),
        "target_trigger_evaluable_candidates": len(trigger_known),
        "target_triggered_candidates": sum(
            item.get("target_vulnerability_triggered") is True
            for item in trigger_known
        ),
        "target_trigger_rate": (
            sum(
                item.get("target_vulnerability_triggered") is True
                for item in trigger_known
            ) / len(trigger_known)
            if trigger_known else None
        ),
    }


def _mean_nested(rows, outer, key):
    values = [
        (row.get(outer) or {}).get(key)
        for row in rows
        if isinstance(row.get(outer), dict) and (row.get(outer) or {}).get(key) is not None
    ]
    return (sum(values) / len(values)) if values else None


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [item[key] for item in rows if item.get(key) is not None]
    return sum(values) / len(values) if values else None


def _mean_stage(rows: list[dict[str, Any]], stage: str) -> float | None:
    values = [
        (item.get("stage_coverage") or {}).get(stage)
        for item in rows
        if (item.get("stage_coverage") or {}).get(stage) is not None
    ]
    return sum(bool(value) for value in values) / len(values) if values else None


def _sum_int(rows: list[dict[str, Any]], key: str) -> int:
    return sum(int(item.get(key) or 0) for item in rows)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _propagation_chain_names(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        diagnostics = row.get("diagnostics")
        if not isinstance(diagnostics, dict):
            continue
        for chain in diagnostics.get("propagation") or []:
            if not isinstance(chain, dict) or chain.get("available") is False:
                continue
            name = str(chain.get("chain") or "unknown")
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-gt", action="store_true")
    parser.add_argument(
        "--relaxed-analysis-quality",
        action="store_true",
        help=(
            "Score structurally valid analysis.json artifacts even when the "
            "quality lint rejects anchors such as harness or README paths."
        ),
    )
    parser.add_argument("--model", action="append")
    parser.add_argument("--sample-id", action="append")
    parser.add_argument(
        "--sample-list",
        type=Path,
        help="newline-delimited sample ids to evaluate",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    sample_ids = set(args.sample_id or [])
    if args.sample_list:
        sample_ids.update(
            line.strip()
            for line in args.sample_list.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    report = (
        audit_gt()
        if args.audit_gt
        else evaluate_batch(
            args.model or [],
            sample_ids,
            require_analysis_quality=not args.relaxed_analysis_quality,
        )
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report.get("summary", {
        "samples": report.get("samples"),
        "conditions": report.get("conditions"),
        "samples_with_errors": report.get("samples_with_errors"),
        "runtime_probe_contract_compiled_samples": report.get(
            "runtime_probe_contract_compiled_samples"
        ),
    }), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
