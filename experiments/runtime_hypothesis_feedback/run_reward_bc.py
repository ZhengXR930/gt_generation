#!/usr/bin/env python3
"""Run the preregistered closed-book no-reward versus V6-reward holdout."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from experiments.runtime_hypothesis_feedback.run_reward_ab import (
    assess,
    proxy_health,
    read_manifest,
    run_monitored,
    write_summary,
)

# Frozen before observing any B/C outcomes. It is disjoint from the V6/V7
# protocol-selection set and spans 20 projects plus all eight vulnerability
# class labels available among eligible ARVO samples with historical runs.
HOLDOUT_IDS = [
    "10129", "10252", "10676", "10865", "12241",
    "12595", "1454", "30271", "10136", "10486",
    "1065", "10841", "10864", "10999", "11244",
    "11517", "11752", "12419", "12420", "12466",
]


def namespace(condition: str, repetition: int) -> str:
    base = f"condition_{condition}_standard_prompt_submit_tool_guard"
    if condition == "c":
        base += "_lightweight_reward_reward_v6"
    return f"{base}_reward_bc_r{repetition}"


def sample_dir(condition: str, repetition: int, arvo_id: str) -> Path:
    return HERE / "results" / namespace(condition, repetition) / f"arvo_{arvo_id}"


def schedule(repetitions: list[int]) -> list[tuple[int, str, str]]:
    result: list[tuple[int, str, str]] = []
    for repetition in repetitions:
        for index, arvo_id in enumerate(HOLDOUT_IDS):
            order = ("b", "c") if (index + repetition) % 2 else ("c", "b")
            result.extend((repetition, condition, arvo_id) for condition in order)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", default="1")
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--model", default="deepseek/deepseek-chat")
    parser.add_argument("--protocol-retries", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    repetitions = sorted({int(x) for x in args.repetitions.split(",") if x})
    if not repetitions or any(x < 1 for x in repetitions):
        parser.error("--repetitions must contain positive integers")

    proxy_health("v6")
    summary_path = HERE / "results" / "reward_v6_bc_holdout_summary.json"
    summary = read_manifest(summary_path) or {
        "experiment": "matched_closed_book_no_reward_vs_v6_reward",
        "holdout_ids": [f"arvo_{item}" for item in HOLDOUT_IDS],
        "selection_frozen_before_outcomes": True,
        "disjoint_from_protocol_selection": True,
        "model": args.model,
        "max_iter": args.max_iter,
        "uses_hidden_gt": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runs": [],
    }
    known = {
        (r.get("repetition"), r.get("condition"), r.get("sample_id"))
        for r in summary.get("runs", [])
        if (r.get("assessment") or {}).get("complete")
        and (r.get("assessment") or {}).get("evaluation_valid") is True
    }
    env = dict(os.environ)
    env.update({
        "OPENHANDS_PYTHON": (
            "/home/xinran/.cache/pypoetry/virtualenvs/"
            "openhands-ai-pW2ZHCQY-py3.12/bin/python"
        ),
        "HYPOTHESIS_LIGHTWEIGHT_REWARD": "1",
        "HYPOTHESIS_REWARD_MODEL": "deepseek-chat",
    })

    for repetition, condition, arvo_id in schedule(repetitions):
        key = (repetition, condition, f"arvo_{arvo_id}")
        destination = sample_dir(condition, repetition, arvo_id)
        existing = assess(destination)
        if not args.force and (
            key in known
            or (existing["complete"] and existing["evaluation_valid"])
        ):
            print(f"skip complete {key}", flush=True)
            continue
        command = [
            sys.executable, str(HERE / "run_experiment.py"),
            "--condition", condition,
            "--arvo-id", arvo_id,
            "--max-iter", str(args.max_iter),
            "--timeout", str(args.timeout),
            "--model", args.model,
            "--submit-candidate-tool",
            "--terminal-guard",
            "--reward-protocol", "v6",
            "--feedback-server", "http://host.docker.internal:8767",
            "--result-suffix", f"reward_bc_r{repetition}",
        ]
        for execution_attempt in range(1, args.protocol_retries + 2):
            log_path = HERE / "runs" / (
                f"bc_r{repetition}_{arvo_id}_{condition}_try{execution_attempt}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            started = datetime.now(timezone.utc).isoformat()
            return_code, protocol_invalid, invalid_reason = run_monitored(
                command, log_path, env
            )
            result = assess(destination)
            record: dict[str, Any] = {
                "repetition": repetition,
                "condition": condition,
                "reward_enabled": condition == "c",
                "reward_protocol": "v6" if condition == "c" else None,
                "sample_id": f"arvo_{arvo_id}",
                "execution_attempt": execution_attempt,
                "protocol_invalid": protocol_invalid,
                "invalid_reason": invalid_reason,
                "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "return_code": return_code,
                "log_path": str(log_path),
                "result_dir": str(destination),
                "assessment": result,
            }
            summary.setdefault("runs", []).append(record)
            write_summary(summary_path, summary)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            if not protocol_invalid:
                break

    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_summary(summary_path, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
