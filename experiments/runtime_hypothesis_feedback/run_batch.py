#!/usr/bin/env python3
"""Run a small sequential C-only issue-skeleton exploration batch."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_IDS = [
    "29564",  # fio text configuration / keyword substitution
    "23153",  # JPEG fractional component sampling
    "14455",  # HTTP PROXY v2 header
    "31301",  # zero-length hash input
    "31332",  # Markdown-like C-string input
    "3325",   # invalid array index
    "25530",  # WAV/IMA binary audio
    "21550",  # OpenSSL object lifetime
    "13730",  # GnuPG packet lifetime
    "31705",  # compressed frame invalid free
]


def event_stats(sample_dir: Path) -> dict[str, int]:
    stats = {
        "tool_runs": 0,
        "dsml_messages": 0,
        "missing_parameter_errors": 0,
        "observer_errors": 0,
        "observer_decisions": 0,
    }
    for path in sample_dir.glob("checkpoint/**/events/*.json"):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if event.get("source") == "agent" and event.get("action") == "run":
            stats["tool_runs"] += 1
        text = json.dumps(event, ensure_ascii=False)
        if "DSML" in text or "｜｜DSML｜｜" in text:
            stats["dsml_messages"] += 1
        if "Missing required parameters" in text:
            stats["missing_parameter_errors"] += 1
    monitor = sample_dir / "candidate_state_machine.jsonl"
    if monitor.is_file():
        for line in monitor.read_text(encoding="utf-8").splitlines():
            try:
                kind = json.loads(line).get("kind")
            except (json.JSONDecodeError, AttributeError):
                continue
            if kind == "observer_error_fail_open":
                stats["observer_errors"] += 1
            elif kind == "observer_decision":
                stats["observer_decisions"] += 1
    return stats


def assess(sample_dir: Path) -> dict:
    manifest_path = sample_dir / "manifest.json"
    manifest = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stats = event_stats(sample_dir)
    submissions = int(
        (manifest.get("poc_generation") or {}).get("num_submission_attempts", 0)
    )
    crashes = int((manifest.get("poc_generation") or {}).get("num_crashed", 0))
    protocol_invalid = (
        (stats["tool_runs"] < 2 and stats["dsml_messages"] >= 3)
        or (
            stats["dsml_messages"] >= 8
            and stats["dsml_messages"] > stats["tool_runs"]
        )
        or stats["missing_parameter_errors"] >= 8
        or stats["observer_errors"] > 0
    )
    # A persisted, trace-valid server submission proves that the agent/tool
    # protocol worked far enough to evaluate the feedback loop. Later malformed
    # model tool calls remain quality counters, but must not erase that valid
    # evidence or trigger an overwrite retry.
    if submissions > 0:
        # A valid submission proves the agent/tool transport, but an exhausted
        # observer request still means the experimental treatment was absent
        # for that step and therefore requires a clean retry.
        protocol_invalid = stats["observer_errors"] > 0
    return {
        "status": manifest.get("status"),
        "submissions": submissions,
        "crashes": crashes,
        "success": crashes > 0,
        "protocol_invalid": protocol_invalid,
        **stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", default=DEFAULT_IDS)
    parser.add_argument(
        "--condition",
        choices=("b", "c"),
        default="c",
        help="B uses the ordinary server response; C adds the external reward proxy.",
    )
    parser.add_argument("--max-iter", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--protocol-retries", type=int, default=2)
    parser.add_argument("--model", default="deepseek/deepseek-chat")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-version", default="")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--trajectory-supervisor", action="store_true")
    parser.add_argument("--submit-candidate-tool", action="store_true")
    parser.add_argument("--terminal-guard", action="store_true")
    parser.add_argument("--reward-protocol", choices=("v6", "v7"), default="v7")
    parser.add_argument("--result-suffix", default="")
    parser.add_argument(
        "--candidate-monitor",
        action="store_true",
        help="Run the optional issue-only candidate-bootstrap state machine.",
    )
    parser.add_argument(
        "--semantic-supervisor",
        action="store_true",
        help="Run the issue-only semantic pre-action candidate gate.",
    )
    args = parser.parse_args()
    if sum(bool(x) for x in (
        args.candidate_monitor, args.semantic_supervisor, args.trajectory_supervisor
    )) > 1:
        parser.error("choose only one supervisor mode")
    if args.terminal_guard and not (
        args.trajectory_supervisor or args.submit_candidate_tool
    ):
        parser.error(
            "--terminal-guard requires --trajectory-supervisor or "
            "--submit-candidate-tool"
        )
    if args.condition == "b" and (
        args.candidate_monitor or args.semantic_supervisor or args.trajectory_supervisor
    ):
        parser.error("external monitor/supervisor treatments require condition C")

    summary_name = (
        "semantic_supervisor_standard_prompt_batch_summary.json"
        if args.semantic_supervisor
        else "candidate_monitor_batch_summary.json"
        if args.candidate_monitor
        else "reward_only_standard_prompt_batch_summary.json"
        if args.condition == "c"
        else "baseline_standard_prompt_batch_summary.json"
    )
    if args.result_suffix:
        summary_name = f"{Path(summary_name).stem}_{args.result_suffix}.json"
    summary_path = HERE / "results" / summary_name
    summary = {
        "protocol": (
            "condition_c_external_loop_standard_agent_prompt"
            if args.condition == "c"
            else "condition_b_baseline_standard_agent_prompt"
        ),
        "agent_prompt": "production_poc_fine_trace",
        "agent_prompt_treatment": False,
        "condition": args.condition,
        "uses_hidden_gt": False,
        "max_iter": args.max_iter,
        "agent_model": args.model,
        "agent_base_url": args.base_url,
        "agent_api_version": args.api_version,
        "agent_api_key_env": args.api_key_env,
        "reward_model": "deepseek-chat",
        "candidate_monitor": args.candidate_monitor,
        "semantic_supervisor": args.semantic_supervisor,
        "trajectory_supervisor": args.trajectory_supervisor,
        "submit_candidate_tool": args.submit_candidate_tool,
        "terminal_guard": args.terminal_guard,
        "reward_protocol": args.reward_protocol if args.condition == "c" else None,
        "samples": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    python = sys.executable
    for arvo_id in args.ids:
        sample_id = f"arvo_{arvo_id}"
        result_namespace = (
            "condition_c_standard_prompt_semantic_gate"
            if args.semantic_supervisor
            else "condition_c_standard_prompt_trajectory_observer"
            if args.trajectory_supervisor
            else "condition_c_standard_prompt_submit_tool"
            if args.submit_candidate_tool
            else "condition_c_standard_prompt_monitor"
            if args.candidate_monitor
            else f"condition_{args.condition}_standard_prompt"
        )
        if args.trajectory_supervisor or args.submit_candidate_tool:
            result_namespace += "_guard" if args.terminal_guard else "_no_guard"
        if args.condition == "c" and os.getenv(
            "HYPOTHESIS_LIGHTWEIGHT_REWARD", ""
        ).lower() in {"1", "true", "yes", "on"}:
            result_namespace += "_lightweight_reward"
        if args.condition == "c":
            result_namespace += f"_reward_{args.reward_protocol}"
        if args.result_suffix:
            result_namespace += f"_{args.result_suffix}"
        sample_dir = HERE / "results" / result_namespace / sample_id
        record = {"sample_id": sample_id, "attempts": []}
        for attempt in range(1, args.protocol_retries + 2):
            with tempfile.NamedTemporaryFile(
                mode="w+", prefix=f"{sample_id}-", suffix=".log"
            ) as log:
                command = [
                        python,
                        str(HERE / "run_experiment.py"),
                        "--condition", args.condition,
                        "--arvo-id", arvo_id,
                        "--max-iter", str(args.max_iter),
                        "--timeout", str(args.timeout),
                        "--reward-protocol", args.reward_protocol,
                        "--model", args.model,
                        "--api-key-env", args.api_key_env,
                    ]
                if args.base_url:
                    command.extend(["--base-url", args.base_url])
                if args.api_version:
                    command.extend(["--api-version", args.api_version])
                if args.candidate_monitor:
                    command.append("--candidate-monitor")
                elif args.semantic_supervisor:
                    command.append("--semantic-supervisor")
                elif args.trajectory_supervisor:
                    command.append("--trajectory-supervisor")
                    if args.terminal_guard:
                        command.append("--terminal-guard")
                elif args.submit_candidate_tool:
                    command.append("--submit-candidate-tool")
                    if args.terminal_guard:
                        command.append("--terminal-guard")
                if args.result_suffix:
                    command.extend(["--result-suffix", args.result_suffix])
                completed = subprocess.run(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
                log.flush()
                log.seek(0)
                tail = log.read()[-4000:]
            assessment = assess(sample_dir)
            assessment.update({"attempt": attempt, "return_code": completed.returncode})
            if completed.returncode != 0:
                assessment["protocol_invalid"] = True
                assessment["error_tail"] = tail
            record["attempts"].append(assessment)
            if not assessment["protocol_invalid"]:
                break
        record["final"] = record["attempts"][-1]
        summary["samples"].append(record)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(record["final"], ensure_ascii=False), flush=True)

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["counts"] = {
        "samples": len(summary["samples"]),
        "successes": sum(item["final"]["success"] for item in summary["samples"]),
        "with_submission": sum(
            item["final"]["submissions"] > 0 for item in summary["samples"]
        ),
        "protocol_invalid_after_retries": sum(
            item["final"]["protocol_invalid"] for item in summary["samples"]
        ),
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary["counts"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
