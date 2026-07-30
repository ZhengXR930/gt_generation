#!/usr/bin/env python3
"""Wait for the two 100-sample batches, then repair every invalid result.

Repair runs are deliberately serial: the initial batch showed correlated
OpenHands runtime disconnects while two large ARVO jobs were active together.
Each rerun replaces the exact model/sample result in place.
"""

import json
import time
from pathlib import Path

from rerun_model_batches import (
    HERE,
    LOG_ROOT,
    RESULTS_ROOT,
    load_config,
    result_is_complete,
    run_one,
)


SUMMARY = LOG_ROOT / "rerun_100_models.jsonl"
REPAIR_LOG = LOG_ROOT / "repair_invalid_results.jsonl"
POLL_SECONDS = 30
RETRY_DELAY_SECONDS = 60


def configured_jobs() -> list[tuple[dict, str]]:
    deepseek_first = load_config("poc_config.deepseek_50.json")
    deepseek_second = load_config("poc_config.deepseek_additional50.json")
    gpt = load_config("poc_config.gpt54_mini_additional100.json")
    deepseek = dict(deepseek_first)
    deepseek["samples"] = list(
        dict.fromkeys(deepseek_first["samples"] + deepseek_second["samples"])
    )
    # Infrastructure failures should receive fresh full-episode retries before
    # being left for another repair round.
    deepseek["max_attempts"] = 3
    gpt["max_attempts"] = 3
    return (
        [(deepseek, sample) for sample in deepseek["samples"]]
        + [(gpt, sample) for sample in gpt["samples"]]
    )


def initial_completed_count(targets: set[tuple[str, str]]) -> int:
    latest = {}
    if SUMMARY.is_file():
        for line in SUMMARY.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (record.get("model"), record.get("sample"))
            if key in targets:
                latest[key] = record
    return len(latest)


def append_record(record: dict) -> None:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    with REPAIR_LOG.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    jobs = configured_jobs()
    targets = {
        (config["results_namespace"], sample) for config, sample in jobs
    }
    while True:
        completed = initial_completed_count(targets)
        print(
            json.dumps(
                {"phase": "waiting_for_initial_batch", "completed": completed},
                ensure_ascii=False,
            ),
            flush=True,
        )
        if completed == len(targets):
            break
        time.sleep(POLL_SECONDS)

    repair_round = 0
    while True:
        invalid = [
            (config, sample)
            for config, sample in jobs
            if not result_is_complete(
                RESULTS_ROOT / config["results_namespace"] / sample
            )
        ]
        if not invalid:
            final = {
                "phase": "repair_complete",
                "valid": len(jobs),
                "total": len(jobs),
                "rounds": repair_round,
            }
            append_record(final)
            print(json.dumps(final, ensure_ascii=False), flush=True)
            return 0

        repair_round += 1
        start = {
            "phase": "repair_round",
            "round": repair_round,
            "invalid": len(invalid),
        }
        append_record(start)
        print(json.dumps(start, ensure_ascii=False), flush=True)
        for config, sample in invalid:
            record = run_one(config, sample)
            result_dir = (
                RESULTS_ROOT / config["results_namespace"] / sample
            )
            record.update(
                {
                    "phase": "repair_result",
                    "round": repair_round,
                    "valid_after_run": result_is_complete(result_dir),
                }
            )
            append_record(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
        time.sleep(RETRY_DELAY_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
