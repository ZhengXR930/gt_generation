#!/usr/bin/env python3
"""Rerun only reachability ledgers created with the invalid ``-runs=0`` argv.

This reuses frozen PoCs and vulnerable ARVO images.  It does not run the coding
agent, rebuild GT, or compile a target.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from reachability.arvo_gdb import prepare_arvo_target, run_arvo_gdb
from reachability.core import evaluate_r1_r5
from reachability.engine import extract_reachability_checkpoints


REPO_ROOT = Path(__file__).resolve().parents[2]
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"
GT_RESULTS = REPO_ROOT / "gt_results"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _is_legacy(output_dir: Path) -> bool:
    path = output_dir / "gdb_command.json"
    if not path.is_file():
        return False
    try:
        return "-runs=0" in (_load(path).get("command") or [])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def discover(models: set[str]) -> list[tuple[str, str, Path, list[Path]]]:
    groups = []
    for model_dir in sorted(POC_RESULTS.iterdir()):
        if not model_dir.is_dir() or model_dir.name.startswith("_"):
            continue
        if models and model_dir.name not in models:
            continue
        for sample_dir in sorted(model_dir.iterdir()):
            root = sample_dir / "reachability"
            if not root.is_dir():
                continue
            outputs = sorted(path for path in root.iterdir() if _is_legacy(path))
            if outputs:
                groups.append(
                    (model_dir.name, sample_dir.name, sample_dir, outputs)
                )
    return groups


def _candidate_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("representative_attempt_id") or ""): item
        for item in manifest.get("deduplicated_pocs", [])
        if isinstance(item, dict) and item.get("representative_attempt_id")
    }


def _image(sample_id: str, manifest: dict[str, Any]) -> str:
    arvo_id = str(manifest.get("arvo_id") or "").strip()
    if not arvo_id and sample_id.startswith("arvo_"):
        arvo_id = sample_id.removeprefix("arvo_")
    if not arvo_id:
        raise ValueError("not an ARVO sample")
    return f"n132/arvo:{arvo_id}-vul"


def rerun_group(
    group: tuple[str, str, Path, list[Path]],
    *,
    timeout: int,
    debugger_image: str,
) -> dict[str, Any]:
    model, sample_id, sample_dir, outputs = group
    manifest = _load(sample_dir / "manifest.json")
    candidates = _candidate_map(manifest)
    gt = _load(GT_RESULTS / sample_id / "ground_truth.json")
    checkpoints = extract_reachability_checkpoints(gt)
    completed = 0
    errors = []
    with prepare_arvo_target(_image(sample_id, manifest)) as prepared:
        for output_dir in outputs:
            attempt_id = output_dir.name
            candidate = candidates.get(attempt_id)
            if candidate is None:
                errors.append(f"{attempt_id}: candidate missing from manifest")
                continue
            relative = str(candidate.get("representative_poc_path") or "")
            poc_path = sample_dir / relative
            if not poc_path.is_file():
                errors.append(f"{attempt_id}: PoC missing")
                continue
            try:
                command_result, hits, checked = run_arvo_gdb(
                    prepared=prepared,
                    poc_path=poc_path,
                    checkpoints=checkpoints,
                    output_dir=output_dir,
                    repo_root=REPO_ROOT,
                    timeout=timeout,
                    debugger_image=debugger_image,
                    max_hits_per_event=1,
                )
                runtime_relative = str(
                    candidate.get("representative_runtime_output_path") or ""
                )
                runtime_path = sample_dir / runtime_relative
                runtime_text = (
                    runtime_path.read_text(encoding="utf-8", errors="replace")
                    if runtime_path.is_file()
                    else None
                )
                report = evaluate_r1_r5(
                    gt=gt,
                    hits=hits if checked else None,
                    sanitizer_trace=runtime_text,
                    checkpoints=checkpoints,
                )
                report.update({
                    "model": model,
                    "sample_id": sample_id,
                    "attempt_id": attempt_id,
                    "gdb_returncode": command_result.returncode,
                })
                (output_dir / "reachability_report.json").write_text(
                    json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                if checked:
                    completed += 1
                else:
                    errors.append(f"{attempt_id}: invalid GDB execution")
            except Exception as exc:  # noqa: BLE001 - isolate one frozen PoC
                errors.append(f"{attempt_id}: {type(exc).__name__}: {exc}")
    return {
        "model": model,
        "sample_id": sample_id,
        "requested": len(outputs),
        "completed": completed,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append")
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--debugger-image", default="gt-memory-env:latest")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    groups = discover(set(args.model or []))
    results = []
    with ThreadPoolExecutor(max_workers=max(args.parallel, 1)) as executor:
        futures = {
            executor.submit(
                rerun_group,
                group,
                timeout=args.timeout,
                debugger_image=args.debugger_image,
            ): group
            for group in groups
        }
        for future in as_completed(futures):
            model, sample_id, _, outputs = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - preserve batch progress
                result = {
                    "model": model,
                    "sample_id": sample_id,
                    "requested": len(outputs),
                    "completed": 0,
                    "errors": [f"{type(exc).__name__}: {exc}"],
                }
            results.append(result)
            print(
                f"{model}/{sample_id}: {result['completed']}/"
                f"{result['requested']} repaired, errors={len(result['errors'])}",
                flush=True,
            )
    report = {
        "protocol": "rerun-invalid-runs-arg-v1",
        "groups": len(groups),
        "requested": sum(item["requested"] for item in results),
        "completed": sum(item["completed"] for item in results),
        "errors": sum(len(item["errors"]) for item in results),
        "results": sorted(results, key=lambda item: (item["model"], item["sample_id"])),
    }
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: report[key] for key in ("groups", "requested", "completed", "errors")}))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
