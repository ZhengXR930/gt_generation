#!/usr/bin/env python3
"""Run every sample from one PoC-generation JSON config."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rerun_model_batches import LOG_ROOT, load_config, run_one

GT_ROOT = Path(__file__).resolve().parents[2]
COMPLETE_PACKAGE_FILES = (
    "ground_truth.json",
    "verified_invariants.json",
    "verified_assertions.json",
    "field_bindings.json",
    "event_locations.json",
)


def complete_gt_samples() -> list[str]:
    selected = []
    for result_dir in sorted(GT_ROOT.joinpath("gt_results").iterdir()):
        if not result_dir.is_dir():
            continue
        if (
            result_dir.name.startswith("_")
            or ".repair-staging" in result_dir.name
            or ".repair-backup" in result_dir.name
        ):
            continue
        if all((result_dir / name).is_file() for name in COMPLETE_PACKAGE_FILES):
            selected.append(result_dir.name)
    return selected


def select_samples(config: dict) -> list[str]:
    samples = config.get("samples") or []
    selector = config.get("sample_selector")
    if not selector:
        return list(dict.fromkeys(samples))
    if selector in {"strict_gt_complete", "strict_gt_complete_non_arvo"}:
        selected = complete_gt_samples()
        selected = list(dict.fromkeys(selected))
        if selector == "strict_gt_complete_non_arvo":
            selected = [sample for sample in selected if not sample.startswith("arvo_")]
        return selected
    if selector != "strict_non_arvo_runtime_recoverable":
        raise ValueError(f"unknown sample_selector: {selector}")
    from openhands_backend.run_local_sample import (
        GT_ROOT as LOCAL_GT_ROOT,
        load_runtime_spec,
    )

    selected = []
    for result_dir in sorted(LOCAL_GT_ROOT.joinpath("gt_results").iterdir()):
        if not result_dir.is_dir() or result_dir.name.startswith("arvo_"):
            continue
        if not all((result_dir / name).is_file() for name in COMPLETE_PACKAGE_FILES):
            continue
        try:
            load_runtime_spec(result_dir)
        except RuntimeError:
            continue
        selected.append(result_dir.name)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Config filename beside this script")
    args = parser.parse_args()

    config = load_config(Path(args.config).name)
    samples = select_samples(config)
    parallel = int(config.get("parallel", 1))
    summary_path = LOG_ROOT / f"{Path(args.config).stem}.jsonl"
    counts = {"complete": 0, "failed": 0, "skipped": 0, "deferred": 0}
    LOG_ROOT.mkdir(parents=True, exist_ok=True)

    with summary_path.open("a", encoding="utf-8") as summary:
        pending = samples
        while pending:
            deferred = []
            with ThreadPoolExecutor(max_workers=parallel) as executor:
                futures = {
                    executor.submit(run_one, config, sample): sample
                    for sample in pending
                }
                for future in as_completed(futures):
                    sample = futures[future]
                    try:
                        record = future.result()
                    except Exception as exc:
                        record = {
                            "model": config["results_namespace"],
                            "sample": sample,
                            "status": "failed",
                            "error": repr(exc),
                        }
                    if record["status"] == "deferred":
                        deferred.append(sample)
                    counts[record["status"]] += 1
                    line = json.dumps(record, ensure_ascii=False)
                    print(line, flush=True)
                    summary.write(line + "\n")
                    summary.flush()
            if deferred and len(deferred) == len(pending):
                time.sleep(30)
            pending = deferred

    print(json.dumps({"final_counts": counts}), flush=True)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
