"""Evaluate every deduplicated submitted PoC against GT reachability anchors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from reachability.arvo_gdb import prepare_arvo_target, run_arvo_gdb
from reachability.core import evaluate_r1_r5
from reachability.engine import extract_reachability_checkpoints

REPO_ROOT = Path(__file__).resolve().parents[2]
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"
GT_RESULTS = REPO_ROOT / "gt_results"
_STAGE_RANK = {
    "reachability_not_checked": -1,
    "parser_not_admitted": 0,
    "source_not_reached": 1,
    "vulnerable_function_not_reached": 2,
    "vulnerable_line_not_reached": 3,
    "R4_reached": 4,
}
_REACHABILITY_FIELDS = (
    "R1_parser_admitted",
    "R2_source_reached",
    "R3_vulnerable_function_reached",
    "R4_vulnerable_line_reached",
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
        arvo_id = sample_id.removeprefix("arvo_")
    return f"n132/arvo:{arvo_id}-vul" if arvo_id else None


def _candidate_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": candidate.get("representative_attempt_id"),
        "sequence_in_run": candidate.get("representative_sequence_in_run"),
        "poc_sha256": candidate.get("poc_sha256"),
        "occurrence_count": candidate.get("occurrence_count"),
        "original_exit_code": candidate.get("representative_vul_exit_code"),
        "poc_path": candidate.get("representative_poc_path"),
        "trace_path": candidate.get("representative_trace_path"),
        "runtime_output_path": candidate.get(
            "representative_runtime_output_path"
        ),
    }


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
    image = _arvo_image(sample_id, manifest)
    if image is None:
        return {
            "model": model,
            "sample_id": sample_id,
            "skipped": "non-ARVO target preparation is not implemented",
        }

    gt = json.loads(gt_path.read_text())
    checkpoints = extract_reachability_checkpoints(gt)
    reachability_root = sample_dir / "reachability"
    rows: list[dict[str, Any]] = []
    try:
        with prepare_arvo_target(image) as prepared:
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
                    gdb_result, hits, checked = run_arvo_gdb(
                        prepared=prepared,
                        poc_path=poc_path,
                        checkpoints=checkpoints,
                        output_dir=output_dir,
                        repo_root=REPO_ROOT,
                        timeout=timeout,
                        debugger_image=debugger_image,
                    )
                    sanitizer_trace = (
                        runtime_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        if runtime_path.is_file()
                        else None
                    )
                    report = evaluate_r1_r5(
                        gt=gt,
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
                        }
                    )
                    (output_dir / "reachability_report.json").write_text(
                        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    row = {
                        **metadata,
                        **{
                            field: report.get(field)
                            for field in _REACHABILITY_FIELDS
                        },
                        "reachability_depth": report.get("reachability_depth"),
                        "target_vulnerability_triggered": report.get(
                            "target_vulnerability_triggered"
                        ),
                        "R5_sanitizer_triggered": report.get(
                            "R5_sanitizer_triggered"
                        ),
                        "failure_stage": report.get("failure_stage"),
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
        "evaluation_protocol": "reachability_per_unique_poc_v1",
        "model": model,
        "sample_id": sample_id,
        "checkpoint_source": {
            "R1": "ground_truth.reachability_checkpoints.parser_admitted",
            "R2": "ground_truth.source",
            "R3_R4": ["ground_truth.root_cause", "ground_truth.sink"],
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append")
    parser.add_argument("--sample-id", action="append")
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument(
        "--debugger-image", default="gt-memory-env:latest"
    )
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
