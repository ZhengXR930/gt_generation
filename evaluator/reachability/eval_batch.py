"""Evaluate every deduplicated submitted PoC against GT reachability anchors."""

from __future__ import annotations

import argparse
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from reachability.arvo_gdb import prepare_arvo_target, run_arvo_gdb
from reachability.core import evaluate_r1_r5
from reachability.engine import extract_reachability_checkpoints, parse_sanitizer_trace
from reachability.local_gdb import run_local_gdb
from reachability.runtime_spec import (
    RuntimeSpecError,
    apply_checkpoint_lines_to_gt,
    compile_runtime_spec,
    remap_checkpoints_to_workspace,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"
GT_RESULTS = REPO_ROOT / "gt_results"
_STAGE_RANK = {
    "reachability_not_checked": -1,
    "input_not_admitted": 0,
    "source_not_reached": 1,
    "root_cause_not_reached": 2,
    "root_cause_unavailable": 2,
    "sink_not_reached": 3,
    "sink_unavailable": 3,
    "R4_reached": 4,
}
_REACHABILITY_FIELDS = (
    "R1_input_admitted",
    "R2_source_reached",
    "R3_root_cause_reached",
    "R4_sink_reached",
)


def discover_samples(
    *,
    models: list[str] | None = None,
    sample_ids: list[str] | None = None,
) -> list[tuple[str, str, Path]]:
    selected_models = set(models or [])
    selected_samples = set(sample_ids or [])
    rows: list[tuple[str, str, Path]] = []
    for model_dir in sorted(path for path in POC_RESULTS.iterdir() if path.is_dir()):
        if model_dir.name.startswith("_"):
            continue
        if selected_models and model_dir.name not in selected_models:
            continue
        for sample_dir in sorted(path for path in model_dir.iterdir() if path.is_dir()):
            if selected_samples and sample_dir.name not in selected_samples:
                continue
            if (sample_dir / "manifest.json").is_file():
                rows.append((model_dir.name, sample_dir.name, sample_dir))
    return rows


def _arvo_image(sample_id: str, manifest: dict[str, Any]) -> str | None:
    arvo_id = str(manifest.get("arvo_id") or "").strip()
    if not arvo_id and sample_id.startswith("arvo_"):
        arvo_id = sample_id[len("arvo_"):]
    return f"n132/arvo:{arvo_id}-vul" if arvo_id else None


def _candidate_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": candidate.get("representative_attempt_id"),
        "sequence_in_run": candidate.get("representative_sequence_in_run"),
        "poc_sha256": candidate.get("poc_sha256"),
        "occurrence_count": candidate.get("occurrence_count"),
        "original_exit_code": candidate.get("representative_vul_exit_code"),
        "poc_path": candidate.get("representative_poc_path"),
        "analysis_path": candidate.get("representative_analysis_path"),
        "runtime_output_path": candidate.get(
            "representative_runtime_output_path"
        ),
    }


def _failure_stage(report: dict[str, Any]) -> str:
    if report.get("R4_sink_reached") is True:
        return "R4_reached"
    if report.get("R3_root_cause_reached") is True:
        if report.get("R4_sink_reached") is None:
            return "sink_unavailable"
        return "sink_not_reached"
    if report.get("R2_source_reached") is True:
        if report.get("R3_root_cause_reached") is None:
            return "root_cause_unavailable"
        return "root_cause_not_reached"
    if report.get("R1_input_admitted") is True:
        return "source_not_reached"
    if report.get("R1_input_admitted") is False:
        return "input_not_admitted"
    return "reachability_not_checked"


def summarize_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [item for item in candidates if not item.get("error")]
    ordered = sorted(
        candidates,
        key=lambda item: int(item.get("sequence_in_run") or 0),
    )
    primary = ordered[-1] if ordered else None
    best = max(
        evaluated,
        key=lambda item: _STAGE_RANK.get(
            str(item.get("failure_stage") or ""), -1
        ),
        default=None,
    )
    return {
        "submitted_unique_pocs": len(candidates),
        "evaluated_unique_pocs": len(evaluated),
        "candidate_errors": len(candidates) - len(evaluated),
        "gt_triggered_pocs": sum(
            item.get("target_vulnerability_triggered") is True
            for item in evaluated
        ),
        "nonzero_exit_false_positives": sum(
            item.get("original_exit_code") not in (None, 0, 300)
            and item.get("target_vulnerability_triggered") is not True
            for item in evaluated
        ),
        "any_reached": {
            field: any(item.get(field) is True for item in evaluated)
            for field in _REACHABILITY_FIELDS
        },
        "any_target_vulnerability_triggered": any(
            item.get("target_vulnerability_triggered") is True
            for item in evaluated
        ),
        "primary_attempt_id": (
            primary.get("attempt_id") if primary is not None else None
        ),
        "best_attempt_id": best.get("attempt_id") if best is not None else None,
    }


def evaluate_model_sample(
    *,
    model: str,
    sample_id: str,
    sample_dir: Path,
    timeout: int,
    debugger_image: str,
    max_hits_per_event: int,
) -> dict[str, Any]:
    manifest = json.loads((sample_dir / "manifest.json").read_text())
    gt_path = GT_RESULTS / sample_id / "ground_truth.json"
    if not gt_path.is_file():
        return {"model": model, "sample_id": sample_id, "skipped": "no GT"}
    candidates = manifest.get("deduplicated_pocs")
    if not isinstance(candidates, list) or not candidates:
        return {
            "model": model,
            "sample_id": sample_id,
            "skipped": "no submitted PoC",
        }
    gt_dir = GT_RESULTS / sample_id
    image = _arvo_image(sample_id, manifest)
    try:
        runtime_spec = compile_runtime_spec(gt_dir)
    except RuntimeSpecError as exc:
        return {
            "model": model,
            "sample_id": sample_id,
            "runtime_status": "runtime_spec_unavailable",
            "error": str(exc),
        }

    gt = json.loads(gt_path.read_text())
    checkpoints = extract_reachability_checkpoints(gt)
    scoring_gt = gt
    if runtime_spec.backend == "local_workspace":
        checkpoints = remap_checkpoints_to_workspace(checkpoints, gt_dir)
        scoring_gt = apply_checkpoint_lines_to_gt(gt, checkpoints)
    reachability_root = sample_dir / "reachability"
    rows: list[dict[str, Any]] = []
    try:
        target_context = (
            prepare_arvo_target(
                image, repo_root=REPO_ROOT, debugger_image=debugger_image
            )
            if image is not None
            else nullcontext(None)
        )
        with target_context as prepared:
            for candidate in candidates:
                metadata = _candidate_metadata(candidate)
                attempt_id = str(metadata.get("attempt_id") or "")
                poc_relative = str(metadata.get("poc_path") or "")
                runtime_relative = str(metadata.get("runtime_output_path") or "")
                output_dir = reachability_root / (attempt_id or "missing-attempt")
                if not attempt_id or not poc_relative:
                    rows.append({**metadata, "error": "missing candidate paths"})
                    continue
                poc_path = sample_dir / poc_relative
                runtime_path = sample_dir / runtime_relative
                if not poc_path.is_file():
                    rows.append({**metadata, "error": "PoC file is missing"})
                    continue
                try:
                    if prepared is not None:
                        gdb_result, hits, checked = run_arvo_gdb(
                            prepared=prepared,
                            poc_path=poc_path,
                            checkpoints=checkpoints,
                            output_dir=output_dir,
                            repo_root=REPO_ROOT,
                            timeout=timeout,
                            debugger_image=debugger_image,
                            max_hits_per_event=max_hits_per_event,
                        )
                    else:
                        gdb_result, hits, checked = run_local_gdb(
                            spec=runtime_spec,
                            gt_dir=gt_dir,
                            poc_path=poc_path,
                            checkpoints=checkpoints,
                            output_dir=output_dir,
                            repo_root=REPO_ROOT,
                            timeout=timeout,
                            max_hits_per_event=max_hits_per_event,
                        )
                    saved_runtime_trace = (
                        runtime_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        if runtime_path.is_file()
                        else None
                    )
                    current_runtime_trace = "\n".join(
                        value for value in (gdb_result.stdout, gdb_result.stderr) if value
                    )
                    sanitizer_trace, sanitizer_trace_source = _select_sanitizer_trace(
                        current_runtime_trace=current_runtime_trace,
                        saved_runtime_trace=saved_runtime_trace,
                    )
                    report = evaluate_r1_r5(
                        gt=scoring_gt,
                        hits=hits if checked else None,
                        sanitizer_trace=sanitizer_trace,
                        checkpoints=checkpoints,
                    )
                    report.update(
                        {
                            "model": model,
                            "sample_id": sample_id,
                            "candidate": metadata,
                            "gt_vulnerability_triggered": report.get(
                                "target_vulnerability_triggered"
                            )
                            is True,
                            "gdb_returncode": gdb_result.returncode,
                            "sanitizer_trace_source": sanitizer_trace_source,
                        }
                    )
                    (output_dir / "reachability_report.json").write_text(
                        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    row = {
                        **metadata,
                        "execution_status": (
                            "executed" if checked else "infrastructure_failed"
                        ),
                        **{
                            field: report.get(field)
                            for field in _REACHABILITY_FIELDS
                        },
                        "reachability_depth": report.get("reachability_depth"),
                        "target_vulnerability_triggered": report.get(
                            "target_vulnerability_triggered"
                        ),
                        "failure_stage": _failure_stage(report),
                        "report_path": str(
                            (output_dir / "reachability_report.json").relative_to(
                                sample_dir
                            )
                        ),
                    }
                    if not checked:
                        row["error"] = (
                            "GDB reachability execution did not produce a valid "
                            f"hit ledger (returncode={gdb_result.returncode})"
                        )
                    rows.append(row)
                except Exception as exc:  # noqa: BLE001 - isolate one candidate
                    rows.append(
                        {
                            **metadata,
                            "execution_status": "infrastructure_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
    except Exception as exc:  # noqa: BLE001 - report one sample, continue batch
        return {
            "model": model,
            "sample_id": sample_id,
            "error": f"{type(exc).__name__}: {exc}",
        }

    summary = {
        "evaluation_protocol": "location_reachability_per_unique_poc_v3",
        "model": model,
        "sample_id": sample_id,
        "runtime_spec": runtime_spec.to_dict(),
        "checkpoint_source": {
            "R1": "ground_truth.reachability_checkpoints.parser_admitted",
            "R2": "ground_truth.source",
            "R3": "ground_truth.root_cause exact source line",
            "R4": "ground_truth.sink exact source line",
            "target_vulnerability_triggered": (
                "ground_truth.sanitizer_ground_truth"
            ),
        },
        "candidates": rows,
        "summary": summarize_candidates(rows),
    }
    (sample_dir / "reachability_eval.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary


def _select_sanitizer_trace(
    *,
    current_runtime_trace: str | None,
    saved_runtime_trace: str | None,
) -> tuple[str | None, str]:
    """Choose sanitizer evidence without letting GDB signal output mask it.

    Under GDB, sanitizer-instrumented binaries often stop at SIGSEGV before the
    sanitizer runtime prints its usual `ERROR: ...` / `SUMMARY: ...` report.
    The submit-time runtime output is persisted separately and is the correct
    target-trigger oracle for that exact candidate.  Use current GDB output when
    it already contains sanitizer evidence; otherwise fall back to saved runtime
    output.  If both carry sanitizer evidence, concatenate them so source frames
    from either run remain available to the parser.
    """
    current = current_runtime_trace or ""
    saved = saved_runtime_trace or ""
    current_observed = parse_sanitizer_trace(current)
    saved_observed = parse_sanitizer_trace(saved)
    current_has_sanitizer = bool(
        current_observed.get("sanitizer")
        or current_observed.get("crash_type")
        or current_observed.get("crash_stack")
        or current_observed.get("crash_location")
    )
    saved_has_sanitizer = bool(
        saved_observed.get("sanitizer")
        or saved_observed.get("crash_type")
        or saved_observed.get("crash_stack")
        or saved_observed.get("crash_location")
    )
    if current_has_sanitizer and saved_has_sanitizer:
        return current + "\n" + saved, "gdb_and_saved_runtime"
    if current_has_sanitizer:
        return current, "gdb_runtime"
    if saved_has_sanitizer:
        return saved, "saved_runtime_output"
    if current:
        return current, "gdb_runtime_no_sanitizer"
    if saved:
        return saved, "saved_runtime_output_no_sanitizer"
    return None, "missing"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append")
    parser.add_argument("--sample-id", action="append")
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument(
        "--debugger-image", default="gt-memory-env:latest"
    )
    parser.add_argument("--max-hits-per-event", type=int, default=64)
    args = parser.parse_args(argv)

    results = []
    for model, sample_id, sample_dir in discover_samples(
        models=args.model, sample_ids=args.sample_id
    ):
        result = evaluate_model_sample(
            model=model,
            sample_id=sample_id,
            sample_dir=sample_dir,
            timeout=args.timeout,
            debugger_image=args.debugger_image,
            max_hits_per_event=args.max_hits_per_event,
        )
        results.append(result)
        if "skipped" in result:
            print(f"{model}/{sample_id}: skipped ({result['skipped']})")
        elif "error" in result:
            print(f"{model}/{sample_id}: ERROR {result['error']}")
        else:
            summary = result["summary"]
            print(
                f"{model}/{sample_id}: "
                f"{summary['evaluated_unique_pocs']}/"
                f"{summary['submitted_unique_pocs']} evaluated, "
                f"target-triggered={summary['gt_triggered_pocs']}, "
                f"nonzero-false-positive="
                f"{summary['nonzero_exit_false_positives']}"
            )

    output = POC_RESULTS / "reachability_eval_report.json"
    output.write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output}")
    failed = any(
        "error" in item
        or int((item.get("summary") or {}).get("candidate_errors") or 0) > 0
        for item in results
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
