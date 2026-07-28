#!/usr/bin/env python3
"""Run the isolated-session GT generation workflow.

CLI-agnostic: a stage can invoke Codex CLI, Claude Code CLI, a shell script, or
a deterministic Python command as long as the command is expressed as a template
in the workflow config. The portable deterministic checks live in `gt_toolkit`
and are called from within stages, keeping this runner thin.

Improvements over the original:
- Freshness check: a required output only counts if it was written during this
  stage run (mtime >= stage start), so a stale file from a previous run cannot
  make a failing stage look successful.
- Retries: `retries` per stage (or `default_retries` in config).
- Resume: `--resume` skips stages already recorded ok in the prior state file.
- Review feedback loop: a review stage may name `feedback_to`; a failed review
  launches a fresh producer CLI session and then a fresh review CLI session.
- Deterministic GT gate: a stage may set `validate_gt: true` to run
  `gt-toolkit validate` on the produced ground_truth.json as part of its success.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parent          # gt_generation/ (roles, workflow, gt_toolkit)
REPO_ROOT = CODE_ROOT.parent                          # repo root (gt_results, dataset, evaluator)
DEFAULT_CONFIG = CODE_ROOT / "workflow.json"


@dataclass
class StageResult:
    name: str
    command: str
    returncode: int | None
    started_at: str
    ended_at: str
    stdout_path: str
    stderr_path: str
    required_outputs_ok: bool
    success_check_ok: bool
    duration_seconds: float = 0.0
    attempts: int = 1
    skipped: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        if self.skipped or self.dry_run:
            return True
        return self.returncode == 0 and self.required_outputs_ok and self.success_check_ok


def main() -> None:
    run_started_monotonic = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True, type=Path, help="Sample metadata JSON.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Workflow config JSON.")
    parser.add_argument("--result-dir", type=Path, help="Override result directory.")
    parser.add_argument("--start-at", default="", help="Skip stages before this stage name.")
    parser.add_argument("--stop-after", default="", help="Stop after this stage name.")
    parser.add_argument("--only", default="", help="Run only one stage.")
    parser.add_argument("--resume", action="store_true", help="Skip stages already ok in the prior state file.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument("--keep-going", action="store_true", help="Continue after a failed stage.")
    args = parser.parse_args()

    config = load_json(args.config)
    sample = load_json(args.sample)
    code_root = CODE_ROOT
    repo_root = REPO_ROOT
    sample_id = str(
        sample.get("sample_id")
        or sample.get("local_sample_id")
        or sample.get("id")
        or args.sample.stem
    )
    result_dir = args.result_dir or Path(str(config.get("result_root", "gt_results"))) / sample_id
    if not result_dir.is_absolute():
        result_dir = repo_root / result_dir
    result_dir.mkdir(parents=True, exist_ok=True)
    # Per-run flags the stage roles read. runtime_disambiguation gates the bounded
    # Stage 02 instrumentation escalation; it is OFF by default so the common path stays
    # short. Rewritten every run so a config change takes effect immediately.
    write_json(result_dir / "run_flags.json", {
        "runtime_disambiguation": bool(config.get("runtime_disambiguation", False)),
    })
    timing_path = result_dir / "generation_timing.json"
    prior_timing = load_json(timing_path) if timing_path.is_file() else {}
    lock_path = result_dir / ".gt_generation.lock"
    lock_handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            f"Another GT runner is already active for {sample_id}: {lock_path}"
        )
    lock_handle.write(f"pid={os.getpid()}\nstarted_at={now()}\n")
    lock_handle.flush()
    logs_dir = generation_logs_path(result_dir, dry_run=args.dry_run)
    logs_dir.mkdir(parents=True, exist_ok=True)

    state_path = generation_state_path(result_dir, dry_run=args.dry_run)
    prior_ok = prior_ok_stages(state_path) if args.resume else set()

    state = {
        "sample_id": sample_id,
        "sample_path": str(args.sample.resolve()),
        "config_path": str(args.config.resolve()),
        "result_dir": str(result_dir),
        "status": "running",
        "started_at": now(),
        "stages": [],
    }
    write_json(state_path, state)

    stages = list(config.get("stages") or [])
    if not stages:
        raise SystemExit(f"No stages in config: {args.config}")

    should_run = make_stage_filter(stages, args.start_at, args.stop_after, args.only)
    failed = False
    for stage in stages:
        name = str(stage.get("name") or "")
        if not name:
            raise SystemExit("Stage without name")
        if not should_run(name) or name in prior_ok:
            append_stage(state_path, skipped_result(name))
            continue

        stage_kwargs = dict(
            stage=stage,
            config=config,
            sample=sample,
            sample_path=args.sample.resolve(),
            sample_id=sample_id,
            repo_root=repo_root,
            code_root=code_root,
            result_dir=result_dir,
            logs_dir=logs_dir,
            dry_run=args.dry_run,
        )
        result = run_stage_with_retries(**stage_kwargs)
        append_stage(state_path, result)
        if not result.ok and stage.get("feedback_to") and not args.dry_run:
            result = run_feedback_loop(
                review_stage=stage,
                initial_result=result,
                stages=stages,
                state_path=state_path,
                stage_kwargs=stage_kwargs,
            )
        if not result.ok:
            failed = True
            if not args.keep_going:
                break

    final_state = load_json(state_path)
    final_state["ended_at"] = now()
    final_state["total_duration_seconds"] = round(
        time.monotonic() - run_started_monotonic, 3
    )
    final_state["status"] = "failed" if failed else "completed"
    write_json(state_path, final_state)
    current_stage_timings = [
        {
            "name": item.get("name"),
            "attempts": item.get("attempts", 1),
            "duration_seconds": item.get("duration_seconds", 0.0),
            "ok": item.get("ok"),
        }
        for item in final_state.get("stages", [])
        if not item.get("skipped")
    ]
    merged_stage_timings = merge_stage_timings(
        prior_timing.get("stages", []), current_stage_timings
    )
    write_json(
        timing_path,
        {
            "sample_id": sample_id,
            "started_at": final_state["started_at"],
            "ended_at": final_state["ended_at"],
            "status": final_state["status"],
            "latest_run_duration_seconds": final_state["total_duration_seconds"],
            "total_duration_seconds": round(
                sum(float(item.get("duration_seconds") or 0.0)
                    for item in merged_stage_timings),
                3,
            ),
            "stages": merged_stage_timings,
        },
    )
    if not failed and config.get("compact_on_success") and not args.dry_run:
        from gt_toolkit.compact_result import compact_result

        compact_report = compact_result(result_dir)
        if not compact_report["ok"]:
            final_state["status"] = "failed"
            final_state["compaction"] = compact_report
            failed = True
    print(json.dumps(final_state, indent=2, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


def run_stage_with_retries(**kwargs: Any) -> StageResult:
    stage = kwargs["stage"]
    config = kwargs["config"]
    retries = int(stage.get("retries", config.get("default_retries", 0)))
    result = run_stage(**kwargs)
    attempt = 1
    while not result.ok and attempt <= retries and not (result.dry_run or result.skipped):
        attempt += 1
        result = run_stage(**kwargs)
        result.attempts = attempt
    return result


def run_feedback_loop(
    *,
    review_stage: dict[str, Any],
    initial_result: StageResult,
    stages: list[dict[str, Any]],
    state_path: Path,
    stage_kwargs: dict[str, Any],
) -> StageResult:
    """Repair with a delta review, then require a fresh final full review.

    The reviewer writes its normal review artifact plus a feedback artifact in the
    result directory. The producer role is responsible for reading that feedback on
    its next isolated invocation. A delta reviewer may confirm the repair direction,
    but only a new full reviewer can accept the resulting GT. No conversational state
    crosses the boundary.
    """
    producer_name = str(review_stage.get("feedback_to") or "")
    producer = next(
        (item for item in stages if str(item.get("name") or "") == producer_name),
        None,
    )
    if producer is None:
        return initial_result
    rounds = int(review_stage.get("feedback_rounds", 1))
    incremental_role = str(review_stage.get("incremental_role") or "").strip()
    result_dir = Path(stage_kwargs["result_dir"])
    logs_dir = Path(stage_kwargs["logs_dir"])
    result = initial_result
    for round_number in range(1, rounds + 1):
        baseline_path = logs_dir / f"ground_truth.before_feedback_{round_number}.json"
        current_gt = result_dir / "ground_truth.json"
        if current_gt.exists():
            baseline_path.write_bytes(current_gt.read_bytes())

        producer_retry = {**producer, "_log_suffix": f"feedback_{round_number}"}
        producer_result = run_stage_with_retries(
            **{**stage_kwargs, "stage": producer_retry}
        )
        append_stage(state_path, producer_result)
        if not producer_result.ok:
            return producer_result

        if baseline_path.exists() and current_gt.exists():
            write_review_delta(
                baseline_path,
                current_gt,
                logs_dir / f"ground_truth.delta_feedback_{round_number}.json",
            )

        if incremental_role:
            incremental_review = {
                **review_stage,
                "role": incremental_role,
                "_log_suffix": f"incremental_feedback_{round_number}",
            }
            result = run_stage_with_retries(
                **{**stage_kwargs, "stage": incremental_review}
            )
            append_stage(state_path, result)
            if not result.ok:
                continue

            final_review = {
                **review_stage,
                "_log_suffix": f"final_feedback_{round_number}",
            }
            result = run_stage_with_retries(
                **{**stage_kwargs, "stage": final_review}
            )
            append_stage(state_path, result)
            if result.ok:
                return result
            continue

        review_retry = {**review_stage, "_log_suffix": f"feedback_{round_number}"}
        result = run_stage_with_retries(**{**stage_kwargs, "stage": review_retry})
        append_stage(state_path, result)
        if result.ok:
            return result
    return result


def write_review_delta(baseline: Path, current: Path, out: Path) -> None:
    before = load_json(baseline)
    after = load_json(current)
    changed_paths: list[str] = []
    collect_changed_json_paths(before, after, "$", changed_paths)
    write_json(
        out,
        {
            "baseline": str(baseline),
            "current": str(current),
            "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            "current_sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
            "changed_count": len(changed_paths),
            "changed_paths": changed_paths,
        },
    )


def collect_changed_json_paths(
    before: Any, after: Any, path: str, changed: list[str]
) -> None:
    if type(before) is not type(after):
        changed.append(path)
        return
    if isinstance(before, dict):
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}"
            if key not in before or key not in after:
                changed.append(child)
            else:
                collect_changed_json_paths(before[key], after[key], child, changed)
        return
    if isinstance(before, list):
        if len(before) != len(after):
            changed.append(f"{path}.length")
        for index, (left, right) in enumerate(zip(before, after)):
            collect_changed_json_paths(left, right, f"{path}[{index}]", changed)
        return
    if before != after:
        changed.append(path)


def run_stage(
    *,
    stage: dict[str, Any],
    config: dict[str, Any],
    sample: dict[str, Any],
    sample_path: Path,
    sample_id: str,
    repo_root: Path,
    code_root: Path,
    result_dir: Path,
    logs_dir: Path,
    dry_run: bool,
) -> StageResult:
    name = str(stage["name"])
    started_at = now()
    started_ts = time.time()
    started_monotonic = time.monotonic()
    log_suffix = str(stage.get("_log_suffix") or "").strip()
    log_stem = name + (f".{log_suffix}" if log_suffix else "")
    stdout_path = logs_dir / f"{log_stem}.stdout.txt"
    stderr_path = logs_dir / f"{log_stem}.stderr.txt"
    variables = build_variables(
        stage=stage, config=config, sample=sample, sample_path=sample_path,
        sample_id=sample_id, repo_root=repo_root, code_root=code_root, result_dir=result_dir,
    )
    command = render(stage_command(stage, config), variables)

    if dry_run:
        stdout_path.write_text(command + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return StageResult(
            name=name, command=command, returncode=None, started_at=started_at,
            ended_at=now(), stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            required_outputs_ok=True, success_check_ok=True, dry_run=True,
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
        )

    proc = subprocess.run(
        command,
        cwd=repo_root,
        shell=True,
        text=True,
        capture_output=True,
        timeout=int(stage.get("timeout") or config.get("default_timeout") or 1800),
        env=stage_env(code_root, stage),
    )
    stdout_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(proc.stderr, encoding="utf-8", errors="replace")

    required_outputs_ok = check_required_outputs(stage, variables, started_ts)
    success_check_ok = check_success(stage, variables) and check_validate_gt(
        stage, variables, repo_root, code_root
    )
    return StageResult(
        name=name, command=command, returncode=proc.returncode, started_at=started_at,
        ended_at=now(), stdout_path=str(stdout_path), stderr_path=str(stderr_path),
        required_outputs_ok=required_outputs_ok, success_check_ok=success_check_ok,
        duration_seconds=round(time.monotonic() - started_monotonic, 3),
    )


def stage_env(code_root: Path, stage: dict[str, Any]) -> dict[str, str]:
    """Stage env with gt_generation/ on PYTHONPATH so `python3 -m gt_toolkit`
    resolves even though stages run with cwd at the repo root."""
    env = {**os.environ, **string_env(stage.get("env") or {})}
    prior = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(code_root) + (os.pathsep + prior if prior else "")
    return env


def stage_command(stage: dict[str, Any], config: dict[str, Any]) -> str:
    command = str(stage.get("command_template") or "")
    if command:
        return command
    agent_command = str(config.get("agent_command_template") or "")
    if not agent_command:
        raise ValueError(
            f"Stage {stage.get('name')} has no command_template and config has no agent_command_template"
        )
    return agent_command


def build_variables(
    *,
    stage: dict[str, Any],
    config: dict[str, Any],
    sample: dict[str, Any],
    sample_path: Path,
    sample_id: str,
    repo_root: Path,
    code_root: Path,
    result_dir: Path,
) -> dict[str, str]:
    role_path = Path(str(stage.get("role") or ""))
    if str(role_path) and not role_path.is_absolute():
        role_path = code_root / role_path  # roles live under gt_generation/
    variables: dict[str, str] = {
        "repo_root": str(repo_root),
        "code_root": str(code_root),
        "sample_id": sample_id,
        "sample_path": str(sample_path),
        "result_dir": str(result_dir),
        "role_file": str(role_path) if str(stage.get("role") or "") else "",
        "state_file": str(result_dir / "gt_generation_state.json"),
        "sample_state": str(result_dir / "sample_state.json"),
        "gt_path": str(result_dir / "ground_truth.json"),
        "static_review_path": str(result_dir / "static_review.json"),
        "trace_feedback_path": str(result_dir / "trace_feedback.json"),
        "reproduction_report_path": str(result_dir / "reproduction_report.json"),
        "verified_invariants_path": str(result_dir / "verified_invariants.json"),
        "candidate_assertions_path": str(result_dir / "candidate_assertions.json"),
        "assertion_results_path": str(result_dir / "assertion_results.json"),
        "perturbation_results_path": str(result_dir / "perturbation_results.json"),
        "verified_assertions_path": str(result_dir / "verified_assertions.json"),
        "reachability_report_path": str(result_dir / "reachability_report.json"),
    }
    for key, value in (config.get("vars") or {}).items():
        variables[key] = str(value)
    for key, value in sample.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            variables[f"sample.{key}"] = "" if value is None else str(value)
    for key, value in (stage.get("vars") or {}).items():
        variables[key] = str(value)
    return variables


def render(template: str, variables: dict[str, str]) -> str:
    result = template
    for key, value in sorted(variables.items(), key=lambda item: -len(item[0])):
        result = result.replace("{" + key + "}", shell_quote_if_needed(value))
    return result


def shell_quote_if_needed(value: str) -> str:
    if value == "":
        return "''"
    if all(ch.isalnum() or ch in "/._:-+=" for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def check_required_outputs(stage: dict[str, Any], variables: dict[str, str], started_ts: float) -> bool:
    """A required output must exist AND be at least as new as the stage start,
    so a leftover file from a previous run cannot count as this run's output."""
    required = stage.get("required_outputs") or []
    for item in required:
        path = Path(render(str(item), variables).strip("'"))
        if not path.exists():
            return False
        # Allow a 2s clock-skew grace window.
        if path.stat().st_mtime + 2 < started_ts:
            return False
    return True


def check_success(stage: dict[str, Any], variables: dict[str, str]) -> bool:
    check = stage.get("success_check") or {}
    if not check:
        return True
    clauses = check.get("all")
    if isinstance(clauses, list):
        return bool(clauses) and all(
            _json_condition_matches(clause, variables, check.get("path"))
            for clause in clauses if isinstance(clause, dict)
        )
    return _json_condition_matches(check, variables)


def _json_condition_matches(
    condition: dict[str, Any], variables: dict[str, str], default_path: Any = ""
) -> bool:
    path_text = str(condition.get("path") or default_path or "")
    field = str(condition.get("field") or "")
    expected = condition.get("equals", True)
    if not path_text or not field:
        return False
    path = Path(render(path_text, variables).strip("'"))
    if not path.exists():
        return False
    try:
        data = load_json(path)
    except Exception:
        return False
    return get_nested(data, field) == expected


def check_validate_gt(
    stage: dict[str, Any], variables: dict[str, str], repo_root: Path, code_root: Path
) -> bool:
    """If a stage sets validate_gt, gate success on `gt-toolkit validate`."""
    if not stage.get("validate_gt"):
        return True
    gt_path = Path(variables["gt_path"])
    if not gt_path.exists():
        return False
    cmd = [sys.executable, "-m", "gt_toolkit", "validate", str(gt_path)]
    if stage.get("validate_strict"):
        cmd.append("--strict")
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, env=stage_env(code_root, stage))
    return proc.returncode == 0


def get_nested(data: dict[str, Any], field: str) -> Any:
    current: Any = data
    for part in field.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def make_stage_filter(stages: list[dict[str, Any]], start_at: str, stop_after: str, only: str):
    names = [str(stage.get("name") or "") for stage in stages]
    if only:
        return lambda name: name == only
    start_index = names.index(start_at) if start_at in names else 0
    stop_index = names.index(stop_after) if stop_after in names else len(names) - 1
    return lambda name: start_index <= names.index(name) <= stop_index


def prior_ok_stages(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    try:
        prior = load_json(state_path)
    except Exception:
        return set()
    return {str(s.get("name")) for s in prior.get("stages", []) if s.get("ok")}


def generation_state_path(result_dir: Path, *, dry_run: bool) -> Path:
    """Keep rendered-command dry runs separate from executed workflow state."""
    filename = "gt_generation_state.dry_run.json" if dry_run else "gt_generation_state.json"
    return result_dir / filename


def generation_logs_path(result_dir: Path, *, dry_run: bool) -> Path:
    """Keep rendered commands from replacing evidence from executed sessions."""
    dirname = "role_logs_dry_run" if dry_run else "role_logs"
    return result_dir / dirname


def skipped_result(name: str) -> StageResult:
    return StageResult(
        name=name, command="", returncode=None, started_at=now(), ended_at=now(),
        stdout_path="", stderr_path="", required_outputs_ok=True, success_check_ok=True,
        skipped=True,
    )


def append_stage(state_path: Path, result: StageResult) -> None:
    state = load_json(state_path)
    state.setdefault("stages", []).append({**result.__dict__, "ok": result.ok})
    state["current_stage"] = result.name
    write_json(state_path, state)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def string_env(raw: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in raw.items()}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def merge_stage_timings(
    prior: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep the latest measured duration for each stage across partial reruns."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in [*prior, *current]:
        name = str(item.get("name") or "")
        if not name:
            continue
        if name not in merged:
            order.append(name)
        merged[name] = dict(item)
    return [merged[name] for name in order]


if __name__ == "__main__":
    main()
