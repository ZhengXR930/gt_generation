#!/usr/bin/env python3
"""Run the matched DeepSeek V6-versus-V7 runtime-feedback experiment."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PRIMARY_IDS = [
    "14467", "10952", "13356", "19385", "22110",
    "11078", "11372", "17171", "18562",
]
POSITIVE_CONTROL_IDS = ["18626"]
PORTS = {"v6": 8767, "v7": 8768}


def result_namespace(protocol: str, repetition: int) -> str:
    return (
        "condition_c_standard_prompt_submit_tool_guard_lightweight_reward_"
        f"reward_{protocol}_ab_r{repetition}"
    )


def result_dir(protocol: str, repetition: int, arvo_id: str) -> Path:
    return HERE / "results" / result_namespace(protocol, repetition) / f"arvo_{arvo_id}"


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def assess(sample_dir: Path) -> dict[str, Any]:
    manifest = read_manifest(sample_dir / "manifest.json")
    generation = manifest.get("poc_generation") or {}
    attempts = generation.get("submission_attempts") or []
    valid = [item for item in attempts if item.get("trace_valid") in {1, True}]
    exits = [item.get("vul_exit_code") for item in valid]
    first_failure = bool(valid and exits[0] in {0, 300})
    crash_sequences = [
        index for index, code in enumerate(exits, 1)
        if code not in {None, 0, 2, 300}
    ]
    unique_hashes = {
        str(item.get("poc_hash")) for item in valid if item.get("poc_hash")
    }
    protocol = manifest.get("reward_protocol") or {}
    external_audit = manifest.get("external_solution_access_audit") or {}
    evaluation_valid = (
        external_audit.get("invalidates_poc_generation_result") is False
    )
    checkpoint_ok = (sample_dir / "checkpoint" / "trajectory").is_file()
    return {
        "status": manifest.get("status"),
        "controller_iterations": manifest.get("effective_controller_iterations"),
        "submission_attempts": len(attempts),
        "valid_submissions": len(valid),
        "unique_pocs": len(unique_hashes),
        "duplicate_submissions": max(0, len(valid) - len(unique_hashes)),
        "success": bool(crash_sequences),
        "first_crash_submission": crash_sequences[0] if crash_sequences else None,
        "failed_first_repaired": bool(first_failure and crash_sequences),
        "trace_valid_rate": (len(valid) / len(attempts)) if attempts else None,
        "checkpoint_ok": checkpoint_ok,
        "recorded_reward_protocol": protocol.get("version"),
        "evaluation_valid": evaluation_valid,
        "external_solution_access_attempted": external_audit.get(
            "attempted_external_access"
        ),
        "external_solution_artifact_acquired": external_audit.get(
            "acquired_external_solution_artifact"
        ),
        "complete": bool(manifest) and checkpoint_ok,
    }


def proxy_health(protocol: str) -> dict[str, Any]:
    url = f"http://127.0.0.1:{PORTS[protocol]}/health"
    with urllib.request.urlopen(url, timeout=10) as response:
        value = json.loads(response.read().decode())
    if value.get("reward_protocol") != protocol:
        raise RuntimeError(f"{url} serves {value.get('reward_protocol')}, expected {protocol}")
    if not (value.get("lightweight_reward_llm") or {}).get("enabled"):
        raise RuntimeError(f"{url} does not have lightweight reward enabled")
    return value


def schedule(repetitions: list[int]) -> list[tuple[int, str, str, bool]]:
    items: list[tuple[int, str, str, bool]] = []
    ids = [(item, False) for item in PRIMARY_IDS] + [
        (item, True) for item in POSITIVE_CONTROL_IDS
    ]
    for repetition in repetitions:
        for index, (arvo_id, positive_control) in enumerate(ids):
            order = ("v6", "v7") if (index + repetition) % 2 else ("v7", "v6")
            for protocol in order:
                items.append((repetition, protocol, arvo_id, positive_control))
    return items


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def run_monitored(
    command: list[str], log_path: Path, env: dict[str, str]
) -> tuple[int, bool, str | None]:
    """Stop only a zero-submission malformed-tool loop; never judge PoC work."""
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
        protocol_invalid = False
        invalid_reason = None
        while process.poll() is None:
            time.sleep(5)
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            submission_observed = '"hypothesis_feedback"' in text
            malformed = text.count("Missing required parameters")
            if not submission_observed and malformed >= 8:
                protocol_invalid = True
                invalid_reason = (
                    "zero-submission OpenHands tool-protocol loop: "
                    f"{malformed} missing-parameter errors"
                )
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=30)
                break
        return_code = process.returncode or 0
    # SIGTERM bypasses the child driver's Python cleanup. Remove only runtime
    # container names explicitly recorded in this episode log.
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    names = sorted(set(re.findall(r"Container started: ([A-Za-z0-9_.-]+)", log_text)))
    if names:
        subprocess.run(
            ["docker", "container", "rm", "--force", *names],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return return_code, protocol_invalid, invalid_reason


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", default="1", help="Comma-separated repetition IDs")
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--model", default="deepseek/deepseek-chat")
    parser.add_argument("--protocol-retries", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    repetitions = sorted({int(item) for item in args.repetitions.split(",") if item})
    if not repetitions or any(item < 1 for item in repetitions):
        parser.error("--repetitions must contain positive integers")

    for protocol in PORTS:
        proxy_health(protocol)

    summary_path = HERE / "results" / "reward_v6_v7_ab_summary.json"
    summary = read_manifest(summary_path)
    if not summary:
        summary = {
            "experiment": "matched_deepseek_reward_v6_v7",
            "primary_ids": [f"arvo_{item}" for item in PRIMARY_IDS],
            "positive_control_ids": [f"arvo_{item}" for item in POSITIVE_CONTROL_IDS],
            "model": args.model,
            "max_iter": args.max_iter,
            "uses_hidden_gt": False,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "runs": [],
        }
    known = {
        (item.get("repetition"), item.get("protocol"), item.get("sample_id"))
        for item in summary.get("runs", [])
        if item.get("assessment", {}).get("complete")
        and item.get("assessment", {}).get("evaluation_valid") is True
    }
    python = sys.executable
    env = dict(os.environ)
    env.update({
        "OPENHANDS_PYTHON": (
            "/home/xinran/.cache/pypoetry/virtualenvs/"
            "openhands-ai-pW2ZHCQY-py3.12/bin/python"
        ),
        "HYPOTHESIS_LIGHTWEIGHT_REWARD": "1",
        "HYPOTHESIS_REWARD_MODEL": "deepseek-chat",
    })

    for repetition, protocol, arvo_id, positive_control in schedule(repetitions):
        key = (repetition, protocol, f"arvo_{arvo_id}")
        sample_dir = result_dir(protocol, repetition, arvo_id)
        existing = assess(sample_dir)
        if not args.force and (
            key in known
            or (existing["complete"] and existing["evaluation_valid"])
        ):
            print(f"skip complete {key}", flush=True)
            continue
        command = [
            python, str(HERE / "run_experiment.py"),
            "--condition", "c",
            "--arvo-id", arvo_id,
            "--max-iter", str(args.max_iter),
            "--timeout", str(args.timeout),
            "--model", args.model,
            "--submit-candidate-tool",
            "--terminal-guard",
            "--reward-protocol", protocol,
            "--feedback-server", f"http://host.docker.internal:{PORTS[protocol]}",
            "--result-suffix", f"ab_r{repetition}",
        ]
        for execution_attempt in range(1, args.protocol_retries + 2):
            log_path = HERE / "runs" / (
                f"ab_r{repetition}_{arvo_id}_{protocol}_try{execution_attempt}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            started = datetime.now(timezone.utc).isoformat()
            return_code, protocol_invalid, invalid_reason = run_monitored(
                command, log_path, env
            )
            assessment = assess(sample_dir)
            record = {
                "repetition": repetition,
                "protocol": protocol,
                "sample_id": f"arvo_{arvo_id}",
                "positive_control": positive_control,
                "execution_attempt": execution_attempt,
                "protocol_invalid": protocol_invalid,
                "invalid_reason": invalid_reason,
                "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "return_code": return_code,
                "log_path": str(log_path),
                "result_dir": str(sample_dir),
                "assessment": assessment,
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
