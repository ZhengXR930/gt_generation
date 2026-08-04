#!/usr/bin/env python3
"""CyberGym submission proxy with frozen, deterministic Reward-Spec feedback."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "evaluator"))

from reachability.arvo_gdb import prepare_arvo_target  # noqa: E402
from experiments.runtime_hypothesis_feedback.gdb_runner import (  # noqa: E402
    run_hypothesis_gdb,
)
from experiments.reward_spec_schema_study.reward_spec_runtime import (  # noqa: E402
    RewardSpecError,
    compile_checkpoints,
    summarize_reward,
)


UPSTREAM = os.getenv("REWARD_SPEC_UPSTREAM", "http://127.0.0.1:8766")
SPEC_ROOT = Path(
    os.getenv(
        "REWARD_SPEC_ROOT",
        REPO_ROOT / "experiments" / "reward_spec_schema_study" / "final_results",
    )
)
FEEDBACK_ROOT = Path(os.getenv("REWARD_SPEC_FEEDBACK_ROOT", HERE / "feedback_logs"))
GDB_TIMEOUT = int(os.getenv("REWARD_SPEC_GDB_TIMEOUT", "180"))
GDB_MAX_HITS = int(os.getenv("REWARD_SPEC_GDB_MAX_HITS", "8"))
DEBUGGER_IMAGE = os.getenv("REWARD_SPEC_DEBUGGER_IMAGE", "gt-memory-env:latest")
_ARVO_TASK = re.compile(r"^arvo:(\d+)$")

app = FastAPI(title="Frozen issue/codebase Reward-Spec feedback")
_target_lock = threading.Lock()
_target_contexts: dict[str, Any] = {}
_targets: dict[str, Any] = {}


def _close_targets() -> None:
    for context in list(_target_contexts.values()):
        try:
            context.__exit__(None, None, None)
        except Exception:
            pass


atexit.register(_close_targets)


def _prepared_target(arvo_id: str):
    with _target_lock:
        if arvo_id not in _targets:
            context = prepare_arvo_target(f"n132/arvo:{arvo_id}-vul")
            _targets[arvo_id] = context.__enter__()
            _target_contexts[arvo_id] = context
        return _targets[arvo_id]


def _attempt_dir(agent_id: str, task_id: str, attempt_id: str) -> Path:
    return FEEDBACK_ROOT / agent_id / task_id.replace(":", "_") / attempt_id


def _prior_feedback(agent_id: str, task_id: str) -> list[dict[str, Any]]:
    task_dir = FEEDBACK_ROOT / agent_id / task_id.replace(":", "_")
    records: list[dict[str, Any]] = []
    if not task_dir.is_dir():
        return records
    for path in task_dir.glob("*/feedback.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                records.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _history(agent_id: str, task_id: str, digest: str) -> tuple[bool, int]:
    records = _prior_feedback(agent_id, task_id)
    duplicate = any(record.get("poc_sha256") == digest for record in records)
    best = max(
        (int(record.get("reward_feedback", {}).get("verified_stage", 0)) for record in records),
        default=0,
    )
    return duplicate, best


def _first_blocked(reward: dict[str, Any]) -> str | None:
    dimensions = reward.get("reward") or {}
    for name in ("admission", "root", "target"):
        if (dimensions.get(name) or {}).get("status") != "satisfied":
            return name
    return None


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "upstream": UPSTREAM,
        "spec_root": str(SPEC_ROOT),
        "uses_hidden_gt": False,
        "uses_llm_judge": False,
    }


@app.post("/submit-vul")
async def submit_vul(
    metadata: str = Form(...),
    file: UploadFile = File(...),
    trace: UploadFile = File(...),
):
    poc_content = await file.read()
    trace_content = await trace.read()
    upstream = requests.post(
        f"{UPSTREAM}/submit-vul",
        data={"metadata": metadata},
        files={
            "file": (file.filename or "poc.bin", poc_content),
            "trace": (
                trace.filename or "candidate_trace.json",
                trace_content,
                "application/json",
            ),
        },
        timeout=300,
    )
    try:
        response = upstream.json()
    except ValueError:
        return JSONResponse(
            {"detail": upstream.text, "upstream_status": upstream.status_code},
            status_code=upstream.status_code,
        )
    if upstream.status_code >= 400:
        return JSONResponse(response, status_code=upstream.status_code)

    try:
        payload = json.loads(metadata)
        task_id = str(payload["task_id"])
        agent_id = str(payload["agent_id"])
        task_match = _ARVO_TASK.fullmatch(task_id)
        if not task_match:
            raise RewardSpecError(f"unsupported task: {task_id}")
        arvo_id = task_match.group(1)
        spec_path = SPEC_ROOT / f"arvo_{arvo_id}.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        checkpoints = compile_checkpoints(spec)
        attempt_id = str(response.get("attempt_id") or "unknown")
        digest = hashlib.sha256(poc_content).hexdigest()
        duplicate, best_before = _history(agent_id, task_id, digest)
        attempt_dir = _attempt_dir(agent_id, task_id, attempt_id)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        poc_path = attempt_dir / "poc.bin"
        poc_path.write_bytes(poc_content)
        (attempt_dir / "reward_spec.json").write_text(
            json.dumps(spec, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _, hits, checked = run_hypothesis_gdb(
            prepared=_prepared_target(arvo_id),
            poc_path=poc_path,
            checkpoints=checkpoints,
            output_dir=attempt_dir / "gdb",
            repo_root=REPO_ROOT,
            timeout=GDB_TIMEOUT,
            debugger_image=DEBUGGER_IMAGE,
            max_hits_per_breakpoint=GDB_MAX_HITS,
        )
        if checked:
            feedback = summarize_reward(spec, hits, response.get("exit_code"))
        else:
            feedback = {
                "source": "frozen_issue_codebase_reward_spec_v1",
                "uses_hidden_gt": False,
                "reward": {},
                "verified_stage": 0,
                "target_runtime": {
                    "exit_code": response.get("exit_code"),
                    "triggered": response.get("exit_code") not in (None, 0, 300),
                },
                "runtime_error": "GDB execution did not complete",
            }
        stage = int(feedback.get("verified_stage", 0))
        feedback["progress"] = max(0, stage - best_before)
        feedback["best_stage_before"] = best_before
        feedback["best_stage_after"] = max(best_before, stage)
        feedback["first_blocked_dimension"] = _first_blocked(feedback)
        feedback["duplicate_poc"] = duplicate
        # Keep the portable submit tool's existing authoritative success parser.
        feedback["target"] = feedback["target_runtime"]
        response["hypothesis_feedback"] = feedback
        record = {
            "task_id": task_id,
            "agent_id": agent_id,
            "attempt_id": attempt_id,
            "poc_sha256": digest,
            "poc_size": len(poc_content),
            "trace_transport_valid": response.get("trace_valid"),
            "reward_spec_path": str(spec_path),
            "uses_hidden_gt": False,
            "uses_llm_judge": False,
            "runtime_checked": checked,
            "reward_feedback": feedback,
        }
        (attempt_dir / "feedback.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        response["hypothesis_feedback"] = {
            "source": "frozen_issue_codebase_reward_spec_v1",
            "uses_hidden_gt": False,
            "uses_llm_judge": False,
            "runtime_error": f"{type(exc).__name__}: {exc}",
            "target": {
                "exit_code": response.get("exit_code"),
                "triggered": response.get("exit_code") not in (None, 0, 300),
            },
        }
    return JSONResponse(response, status_code=upstream.status_code)
