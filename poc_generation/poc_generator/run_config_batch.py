#!/usr/bin/env python3
"""Run every sample from one PoC-generation JSON config."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rerun_model_batches import LOG_ROOT, load_config, run_one


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Config filename beside this script")
    args = parser.parse_args()

    config = load_config(Path(args.config).name)
    samples = list(dict.fromkeys(config["samples"]))
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
