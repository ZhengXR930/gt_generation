"""Deterministic external interpreter for vulnerability-agent trajectories.

The interpreter observes artifacts produced by a coding-agent run and dispatches
non-optional evaluation recorders. It does not decide vulnerability reasoning
labels itself; reasoning labels come from the tested agent's explicit recorder
actions and are exported from the trajectory.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from evaluator.poc_attempt_binder import (
    export_reasoning_artifacts,
    write_bound_poc_attempts,
)
from external_interpreter.raw_context import export_raw_context_artifacts


@dataclass(frozen=True)
class InterpreterConfig:
    trajectory: Path
    out_dir: Path
    gt: Path | None = None
    debug_command: str = ""
    sanitizer_command: str = ""
    timeout: int = 120


def run_interpreter(config: InterpreterConfig) -> dict[str, Any]:
    config.out_dir.mkdir(parents=True, exist_ok=True)
    events_path = config.out_dir / "interpreter_events.jsonl"
    if events_path.exists():
        events_path.unlink()

    reasoning_events, reasoning_state = export_reasoning_artifacts(
        config.trajectory, config.out_dir
    )
    _append_event(
        events_path,
        {
            "type": "reasoning_artifacts_exported",
            "reasoning_events": len(reasoning_events),
            "reasoning_state": str(config.out_dir / "reasoning_state.json"),
        },
    )

    raw_context_events, raw_context_summary = export_raw_context_artifacts(
        config.trajectory, config.out_dir
    )
    _append_event(
        events_path,
        {
            "type": "raw_context_exported",
            "raw_context_events": len(raw_context_events),
            "raw_context_summary": str(config.out_dir / "raw_context_summary.json"),
        },
    )

    attempts_path = config.out_dir / "poc_attempts.json"
    attempts = write_bound_poc_attempts(config.trajectory, attempts_path)
    attempts_jsonl_path = config.out_dir / "poc_attempts.jsonl"
    attempts_jsonl_path.write_text(
        "".join(json.dumps(attempt, ensure_ascii=False) + "\n" for attempt in attempts),
        encoding="utf-8",
    )
    _append_event(
        events_path,
        {
            "type": "poc_attempts_bound",
            "poc_attempts": len(attempts),
            "poc_attempts_json": str(attempts_path),
            "poc_attempts_jsonl": str(attempts_jsonl_path),
        },
    )

    reachability_reports = _run_reachability_for_attempts(
        config=config, attempts=attempts, events_path=events_path
    )

    observer_summary = _run_observer_if_enabled(config, reasoning_state, events_path)

    state = {
        "trajectory": str(config.trajectory),
        "out_dir": str(config.out_dir),
        "reasoning": _summarize_reasoning_state(
            reasoning_state=reasoning_state,
            events_count=len(reasoning_events),
            state_path=config.out_dir / "reasoning_state.json",
        ),
        "poc_attempts": {
            "count": len(attempts),
            "path": str(attempts_path),
            "jsonl_path": str(attempts_jsonl_path),
        },
        "raw_context": raw_context_summary,
        "reachability_reports": reachability_reports,
        "observer": observer_summary,
        "events_path": str(events_path),
    }
    state_path = config.out_dir / "interpreter_state.json"
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return state


def _run_observer_if_enabled(config, reasoning_state, events_path) -> dict[str, Any]:
    """GT-blind citation-grounded observer. Off unless GT_EVAL_RUN_OBSERVER is set
    (it calls an LLM). Failures — incl. model safeguards — are recorded, never fatal."""
    import os
    if not os.getenv("GT_EVAL_RUN_OBSERVER"):
        return {"ran": False, "reason": "GT_EVAL_RUN_OBSERVER not set"}
    try:
        from external_interpreter.observer import litellm_backend, run_observer
        summary = run_observer(
            config.trajectory, config.out_dir, backend=litellm_backend(),
            recorder_state=reasoning_state, skeptic=True,
        )
        _append_event(events_path, {"type": "observer_ran", **{k: summary[k] for k in
                      ("input_events", "nodes", "edges", "citations_dropped", "skeptic_rejected")}})
        return {"ran": True, **summary}
    except Exception as exc:  # safeguard trip, missing key, litellm absent, ...
        _append_event(events_path, {"type": "observer_failed", "error": f"{type(exc).__name__}: {exc}"})
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}


def _summarize_reasoning_state(
    *, reasoning_state: dict[str, Any], events_count: int, state_path: Path
) -> dict[str, Any]:
    coverage = reasoning_state.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    return {
        "events_count": events_count,
        "state_path": str(state_path),
        "selected_event_id": reasoning_state.get("selected_event_id"),
        "selected_snapshot_event_id": reasoning_state.get("selected_snapshot_event_id"),
        "reasoning_complete": bool(reasoning_state.get("reasoning_complete")),
        "next_missing": reasoning_state.get("next_missing"),
        "source_count": len(reasoning_state.get("all_sources") or []),
        "sink_count": len(reasoning_state.get("all_sinks") or []),
        "root_cause_count": len(reasoning_state.get("all_root_causes") or []),
        "edge_count": len(reasoning_state.get("trace") or []),
        "harness_source_warnings": coverage.get("harness_source_warnings", 0),
    }


def _run_reachability_for_attempts(
    *,
    config: InterpreterConfig,
    attempts: list[dict[str, Any]],
    events_path: Path,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    if not config.gt:
        _append_event(
            events_path,
            {
                "type": "reachability_skipped",
                "reason": "missing_gt",
            },
        )
        return reports
    if not (config.debug_command or config.sanitizer_command):
        _append_event(
            events_path,
            {
                "type": "reachability_skipped",
                "reason": "missing_instrumented_command",
            },
        )
        return reports

    for attempt in attempts:
        attempt_id = int(attempt.get("attempt_id") or len(reports) + 1)
        poc_path = _resolve_poc_path(
            str(attempt.get("poc_path") or ""),
            trajectory_dir=config.trajectory.parent,
            out_dir=config.out_dir,
        )
        if poc_path is None:
            report = {
                "attempt_id": attempt_id,
                "status": "skipped",
                "reason": "poc_artifact_unavailable",
                "poc_path": attempt.get("poc_path") or "",
            }
            reports.append(report)
            _append_event(events_path, {"type": "reachability_skipped", **report})
            continue

        out_dir = config.out_dir / "reachability" / f"attempt_{attempt_id}"
        cmd = [
            sys.executable,
            "-m",
            "reachability_eval.cli",
            "--gt",
            str(config.gt),
            "--poc",
            str(poc_path),
            "--out-dir",
            str(out_dir),
            "--timeout",
            str(config.timeout),
        ]
        if config.debug_command:
            cmd.extend(["--debug-command", config.debug_command])
        if config.sanitizer_command:
            cmd.extend(["--sanitizer-command", config.sanitizer_command])
        proc = _run_command(cmd, cwd=Path.cwd(), timeout=config.timeout + 30)
        report_path = out_dir / "reachability_report.json"
        report: dict[str, Any] = {
            "attempt_id": attempt_id,
            "status": "completed" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "report_path": str(report_path),
        }
        if report_path.exists():
            report["report"] = _load_json(report_path)
        else:
            report["stderr_tail"] = proc.stderr[-2000:]
        reports.append(report)
        _append_event(events_path, {"type": "reachability_evaluated", **report})
    return reports


def _resolve_poc_path(
    value: str, *, trajectory_dir: Path, out_dir: Path
) -> Path | None:
    if not value:
        return None
    raw = Path(value)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
        if value.startswith("/workspace/"):
            rel = Path(value).relative_to("/workspace")
            candidates.extend([out_dir / "workspace" / rel, trajectory_dir / rel])
    else:
        candidates.extend([Path.cwd() / raw, trajectory_dir / raw, out_dir / raw])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _run_command(cmd: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
