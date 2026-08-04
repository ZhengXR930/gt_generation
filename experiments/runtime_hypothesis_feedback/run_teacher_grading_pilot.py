#!/usr/bin/env python3
"""Replay frozen historical candidates through the current Reward Agent.

This is deliberately an evaluation-only driver: it does not expose GT or mutate
the historical result directories.  Every replay gets an isolated agent id so
that accumulated candidate state cannot leak between examples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_OUTPUT = HERE / "results" / "teacher_grading_pilot_v1"

CALIBRATION = [
    {
        "name": "admission_arvo_18626",
        "task_id": "arvo:18626",
        "historical_stratum": "admission",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard_lightweight_reward/arvo_18626/submissions/29279bffa88b4ff28a96ff76913e2820",
    },
    {
        "name": "root_arvo_18626",
        "task_id": "arvo:18626",
        "historical_stratum": "root",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard_lightweight_reward/arvo_18626/submissions/79f09aad9756404192ea6f380eaefb73",
    },
    {
        "name": "low_admission_arvo_18626_b",
        "task_id": "arvo:18626",
        "historical_stratum": "admission_location_only",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard_lightweight_reward/arvo_18626/submissions/eb1b8446501d4b198b177d32cd2faa56",
    },
    {
        "name": "low_admission_arvo_18626_c",
        "task_id": "arvo:18626",
        "historical_stratum": "admission_location_only",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard/arvo_18626/submissions/b522fd5bce754bc8884a4fc233465ae7",
    },
    {
        "name": "low_admission_arvo_18626_d",
        "task_id": "arvo:18626",
        "historical_stratum": "admission_location_only",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard/arvo_18626/submissions/1b615a5b5e2a4a31aeee2e68c1f280c3",
    },
    {
        "name": "root_not_reached_arvo_18626_a",
        "task_id": "arvo:18626",
        "historical_stratum": "root_not_reached",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard_lightweight_reward/arvo_18626/submissions/29e11dd3f72b45d4be5ddfc3917dd6dd",
    },
    {
        "name": "root_not_reached_arvo_18626_b",
        "task_id": "arvo:18626",
        "historical_stratum": "root_not_reached",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard/arvo_18626/submissions/2fe2f92315a14dce9e50943fd7c1a362",
    },
    {
        "name": "root_not_reached_arvo_14467",
        "task_id": "arvo:14467",
        "historical_stratum": "root_not_reached",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard_lightweight_reward_reward_v6_ab_r1/arvo_14467/submissions/e3c59553f2f14272afdc15aeeb3c4c57",
    },
    {
        "name": "root_frontier_arvo_11078",
        "task_id": "arvo:11078",
        "historical_stratum": "root_location_reached",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard_lightweight_reward_reward_v6_ab_r1/arvo_11078/submissions/6c2e204d88b64b2089bd691c7732564f",
    },
    {
        "name": "root_frontier_arvo_14467",
        "task_id": "arvo:14467",
        "historical_stratum": "root_location_reached",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard_lightweight_reward_reward_v6_ab_r1/arvo_14467/submissions/092ca0c84bd8492da5c2385f13d899b1",
    },
    {
        "name": "target_arvo_14467",
        "task_id": "arvo:14467",
        "historical_stratum": "target",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard_lightweight_reward_reward_v6_ab_r1/arvo_14467/submissions/ebab834020a54014a771d8c4cadd498e",
    },
    {
        "name": "target_arvo_18626",
        "task_id": "arvo:18626",
        "historical_stratum": "target",
        "source": "experiments/runtime_hypothesis_feedback/results/condition_c_standard_prompt_submit_tool_guard_lightweight_reward_deepseek_recheck/arvo_18626/submissions/47e50b2a7e154553aa2c81f513115ff3",
    },
]

STAGES = ("admission", "root", "propagation", "target")
CONFIRMED = {"confirmed", "triggered"}
ADVICE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:try|change|modify|set|replace|increase|decrease|craft|use)\b",
        r"\bnext (?:try|candidate|step)\b",
        r"\byou should\b",
        r"\b0x[0-9a-f]{2,}\b",
    )
]


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _stage_state(feedback: dict[str, Any]) -> dict[str, str]:
    state = feedback.get("stage_state") or feedback.get("normalized_stage_state")
    return state if isinstance(state, dict) else {}


def _ordered_prefix(state: dict[str, str]) -> tuple[list[str], str | None]:
    prefix: list[str] = []
    frontier = None
    for stage in STAGES:
        if state.get(stage) in CONFIRMED and frontier is None:
            prefix.append(stage)
        elif frontier is None:
            frontier = stage
    return prefix, frontier


def _audit(
    response: dict[str, Any], verifier_record: dict[str, Any], expected: str
) -> dict[str, Any]:
    feedback = response.get("hypothesis_feedback") or {}
    verifier = verifier_record.get("hypothesis_feedback") or {}
    state = _stage_state(feedback)
    prefix, frontier = _ordered_prefix(state)
    diagnosis = feedback.get("reward_agent_diagnosis") or {}
    prose = " ".join(
        str(diagnosis.get(key) or feedback.get(key) or "")
        for key in ("last_confirmed", "first_unresolved", "reason", "error_report")
    )
    downstream_after_gap = False
    gap_seen = False
    for stage in STAGES:
        confirmed = state.get(stage) in CONFIRMED
        if gap_seen and confirmed:
            downstream_after_gap = True
        if not confirmed:
            gap_seen = True
    exit_code = response.get("exit_code")
    target_by_oracle = isinstance(exit_code, int) and exit_code not in {0, 300}
    target_by_reward = state.get("target") in CONFIRMED
    return {
        "historical_stratum": expected,
        "trace_valid": response.get("trace_valid"),
        "exit_code": exit_code,
        "runtime_checked": bool(verifier.get("runtime_checked")),
        "declared_steps": verifier.get("declared_steps"),
        "observed_steps": verifier.get("observed_steps"),
        "exactly_observed_steps": verifier.get("exactly_observed_steps"),
        "mapping_error": (verifier.get("trace_anchor_mapping") or {}).get("error"),
        "stage_state": state,
        "longest_confirmed_prefix": prefix,
        "first_unresolved": frontier,
        "ordered_stage_gate_valid": not downstream_after_gap,
        "target_oracle_consistent": target_by_oracle == target_by_reward,
        "summary_source": feedback.get("summary_source"),
        "reward_agent_called": bool(diagnosis),
        "advice_leakage_flags": [
            pattern.pattern for pattern in ADVICE_PATTERNS if pattern.search(prose)
        ],
        "diagnosis": diagnosis,
    }


def replay(
    entry: dict[str, str], endpoint: str, output: Path, run_id: str
) -> dict[str, Any]:
    source = REPO_ROOT / entry["source"]
    poc = source / "poc.bin"
    trace = source / "candidate_trace.json"
    if not poc.is_file() or not trace.is_file():
        raise FileNotFoundError(f"incomplete frozen candidate: {source}")
    # Parse locally before making any request, so malformed fixtures never reach
    # the runtime service.
    if not isinstance(_json(trace), list):
        raise ValueError(f"trace is not a JSON array: {trace}")
    agent_id = f"teacher_pilot_{run_id}_{entry['name']}"
    checksum = hashlib.sha256(
        f"{entry['task_id']}{agent_id}CyberGym".encode()
    ).hexdigest()
    metadata = json.dumps(
        {"agent_id": agent_id, "task_id": entry["task_id"], "checksum": checksum}
    )
    with poc.open("rb") as poc_file, trace.open("rb") as trace_file:
        reply = requests.post(
            endpoint,
            data={"metadata": metadata},
            files={
                "file": ("poc.bin", poc_file, "application/octet-stream"),
                "trace": ("candidate_trace.json", trace_file, "application/json"),
            },
            timeout=900,
        )
    try:
        response = reply.json()
    except ValueError as exc:
        raise RuntimeError(f"non-JSON response ({reply.status_code}): {reply.text}") from exc
    if reply.status_code >= 400:
        raise RuntimeError(f"HTTP {reply.status_code}: {json.dumps(response)}")
    case_dir = output / entry["name"]
    case_dir.mkdir(parents=True, exist_ok=True)
    attempt_id = str(response.get("attempt_id") or "")
    verifier_path = (
        HERE
        / "feedback_logs"
        / agent_id
        / entry["task_id"].replace(":", "_")
        / attempt_id
        / "feedback.json"
    )
    verifier_record = _json(verifier_path) if verifier_path.is_file() else {}
    record = {
        "fixture": entry,
        "response": response,
        "verifier_record": verifier_record,
        "verifier_record_path": str(verifier_path),
        "verifier_record_persisted": verifier_path.is_file(),
        "audit": _audit(response, verifier_record, entry["historical_stratum"]),
    }
    (case_dir / "replay.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8768/submit-vul")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--run-id", default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    parser.add_argument(
        "--case", action="append", choices=[entry["name"] for entry in CALIBRATION]
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    selected = [
        entry for entry in CALIBRATION
        if not args.case or entry["name"] in set(args.case)
    ]
    for entry in selected:
        print(f"replaying {entry['name']}...", flush=True)
        records.append(replay(entry, args.endpoint, args.output, args.run_id))
    summary = {
        "protocol": "issue-aligned teacher grading calibration v1",
        "run_id": args.run_id,
        "uses_hidden_gt": False,
        "cases": [record["audit"] for record in records],
        "all_runtime_checked": all(
            record["audit"]["runtime_checked"] for record in records
        ),
        "all_ordered": all(
            record["audit"]["ordered_stage_gate_valid"] for record in records
        ),
        "all_target_oracle_consistent": all(
            record["audit"]["target_oracle_consistent"] for record in records
        ),
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
