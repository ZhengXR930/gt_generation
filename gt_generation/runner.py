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
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parent          # gt_generation/ (roles, workflow, gt_toolkit)
REPO_ROOT = CODE_ROOT.parent                          # repo root (gt_results, dataset, evaluator)
DEFAULT_CONFIG = CODE_ROOT / "workflow.json"
FINAL_STAGE = "05_validate"
ASSERTION_PLAN_STAGE = "04_assertion_plan"
VULNERABLE_INSTRUMENTATION_STAGE = "04_instrument_vulnerable"
FIXED_INSTRUMENTATION_STAGE = "04_instrument_fixed"
ASSERTION_EXECUTE_STAGE = "04_assertion_execute"
LEGACY_ASSERTION_STAGE = "04_assertion_validator"
STAGE_ALIASES = {
    LEGACY_ASSERTION_STAGE: ASSERTION_PLAN_STAGE,
}


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
    failure_kind: str = ""

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
    parser.add_argument(
        "--reuse-repair-staging",
        action="store_true",
        help=(
            "Preserve an existing transactional repair staging directory. "
            "Use only to continue accepted earlier stages after a failed attempt."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument("--keep-going", action="store_true", help="Continue after a failed stage.")
    parser.add_argument(
        "--runtime-disambiguation",
        action="store_true",
        help=(
            "Enable the bounded Stage 02 runtime measurement for this sample only; "
            "the workflow default remains unchanged."
        ),
    )
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
    published_result_dir = (
        args.result_dir
        or Path(str(config.get("result_root", "gt_results"))) / sample_id
    )
    if not published_result_dir.is_absolute():
        published_result_dir = repo_root / published_result_dir
    published_result_dir.mkdir(parents=True, exist_ok=True)
    lock_path = published_result_dir.parent / (
        "." + published_result_dir.name + ".gt_generation.lock"
    )
    lock_handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            f"Another GT runner is already active for {sample_id}: {lock_path}"
        )
    lock_handle.write(f"pid={os.getpid()}\nstarted_at={now()}\n")
    lock_handle.flush()
    stages = list(config.get("stages") or [])
    if not stages:
        raise SystemExit(f"No stages in config: {args.config}")
    validate_stage_bounds(stages, args.start_at, args.stop_after, args.only)

    transactional_repair = should_stage_repair(
        published_result_dir, args.start_at, args.only, args.dry_run
    )
    result_dir = published_result_dir
    if transactional_repair:
        result_dir = repair_staging_dir(published_result_dir)
        prepare_transactional_repair(
            published_result_dir,
            result_dir,
            reuse=args.reuse_repair_staging,
        )
    elif args.reuse_repair_staging:
        raise SystemExit(
            "--reuse-repair-staging requires a transactional partial repair"
        )
    install_generation_provenance(result_dir)
    # Per-run flags the stage roles read. runtime_disambiguation gates the bounded
    # Stage 02 instrumentation escalation; it is OFF by default so the common path stays
    # short. Rewritten every run so a config change takes effect immediately.
    write_json(result_dir / "run_flags.json", {
        "runtime_disambiguation": runtime_disambiguation_enabled(
            config, args.runtime_disambiguation
        ),
    })
    timing_path = result_dir / "generation_timing.json"
    prior_timing = load_json(timing_path) if timing_path.is_file() else {}
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

    should_run = make_stage_filter(stages, args.start_at, args.stop_after, args.only)
    active_stages = [
        stage for stage in stages
        if should_run(str(stage.get("name") or ""))
        and str(stage.get("name") or "") not in prior_ok
    ]
    if not args.dry_run and any(
        str(stage.get("name") or "") == "03_trace_review"
        for stage in active_stages
    ):
        clear_stale_feedback_control_files(logs_dir)
    if needs_resume_source_hydration(active_stages, args.dry_run):
        from gt_toolkit.prepare import ensure_arvo_resume_source, is_arvo_sample

        if is_arvo_sample(sample):
            hydration = ensure_arvo_resume_source(sample, result_dir)
            write_json(result_dir / "resume_source_report.json", hydration)
            if not hydration.get("prepared"):
                state["status"] = "failed"
                state["ended_at"] = now()
                state["failure"] = "resume source hydration failed"
                write_json(state_path, state)
                raise SystemExit(
                    f"Cannot run resumed agent stage for {sample_id}: "
                    f"{hydration.get('reason', 'source unavailable')}"
                )
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
        if not args.dry_run:
            prepare_stage_entry(name, result_dir)
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
    repair_publish_allowed = (
        transactional_repair
        and should_run(FINAL_STAGE)
        and FINAL_STAGE not in prior_ok
        and repair_package_ready_to_publish(result_dir)
    )
    if transactional_repair and not failed and not repair_publish_allowed:
        failed = True
        final_state["status"] = "failed"
        final_state["failure"] = (
            "repair package failed the runner-owned commitment/audit publish gate"
        )
        write_json(state_path, final_state)
    if (
        transactional_repair
        and repair_publish_allowed
        and not failed
        and not args.dry_run
    ):
        publish_repair_staging(result_dir, published_result_dir)
        final_state["published_result_dir"] = str(published_result_dir)
        final_state["repair_staging_dir"] = str(result_dir)
        final_state["published"] = True
        write_json(published_result_dir / "gt_generation_state.json", final_state)
    elif transactional_repair:
        final_state["published_result_dir"] = str(published_result_dir)
        final_state["repair_staging_dir"] = str(result_dir)
        final_state["published"] = False
        write_json(state_path, final_state)
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
        prepare_stage_retry(
            str(stage.get("name") or ""),
            Path(kwargs["result_dir"]),
        )
        result = run_stage(**kwargs)
        result.attempts = attempt
    return result


def prepare_stage_retry(stage_name: str, result_dir: Path) -> list[Path]:
    """Remove only outputs owned by the retried stage.

    Stage 04 used to rerun one large agent while leaving a mixture of plan,
    execution and reachability artifacts behind. The freshness gate then marked
    a semantically valid run failed, or worse, let the agent reason from stale
    runtime evidence. Split stages keep their commitments and retry only their
    own outputs.
    """
    owned = {
        ASSERTION_PLAN_STAGE: (
            "candidate_assertions.json",
            "candidate_invariants.json",
            "field_bindings.json",
            "event_locations.json",
            ".assertion_spec_frozen.json",
            "assertion_preflight.json",
        ),
        VULNERABLE_INSTRUMENTATION_STAGE: (
            "vulnerable-instrumentation.patch",
            "vulnerable_instrumentation_preflight.json",
        ),
        FIXED_INSTRUMENTATION_STAGE: (
            "fixed-instrumentation.patch",
            "fixed_instrumentation_preflight.json",
        ),
        ASSERTION_EXECUTE_STAGE: (
            "vulnerable_assertion_trace.txt",
            "fixed_assertion_trace.txt",
            "assertion_results.json",
            "perturbation_results.json",
            "verified_assertions.json",
            "verified_invariants.json",
        ),
        "04_reachability": (
            "reachability_report.json",
        ),
    }
    removed: list[Path] = []
    for name in owned.get(stage_name, ()):
        path = result_dir / name
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(path)
    if stage_name == "04_reachability":
        reachability_dir = result_dir / "reachability"
        if reachability_dir.is_dir():
            shutil.rmtree(reachability_dir)
            removed.append(reachability_dir)
    return removed


def prepare_stage_entry(stage_name: str, result_dir: Path) -> list[Path]:
    """Hide prior evidence from a new split-stage attempt.

    Transactional repairs begin as a copy of the published package. Stage 04A
    must not see old runtime answers, and each deterministic stage must produce
    evidence for the current frozen plan rather than inherit a prior report.
    """
    removed: list[Path] = []
    if stage_name == ASSERTION_PLAN_STAGE:
        for owned_stage in (
            ASSERTION_PLAN_STAGE,
            VULNERABLE_INSTRUMENTATION_STAGE,
            FIXED_INSTRUMENTATION_STAGE,
            ASSERTION_EXECUTE_STAGE,
            "04_reachability",
        ):
            removed.extend(prepare_stage_retry(owned_stage, result_dir))
    elif stage_name == VULNERABLE_INSTRUMENTATION_STAGE:
        for owned_stage in (
            VULNERABLE_INSTRUMENTATION_STAGE,
            FIXED_INSTRUMENTATION_STAGE,
            ASSERTION_EXECUTE_STAGE,
            "04_reachability",
        ):
            removed.extend(prepare_stage_retry(owned_stage, result_dir))
    elif stage_name == FIXED_INSTRUMENTATION_STAGE:
        for owned_stage in (
            FIXED_INSTRUMENTATION_STAGE,
            ASSERTION_EXECUTE_STAGE,
            "04_reachability",
        ):
            removed.extend(prepare_stage_retry(owned_stage, result_dir))
    elif stage_name in {ASSERTION_EXECUTE_STAGE, "04_reachability"}:
        removed.extend(prepare_stage_retry(stage_name, result_dir))
    return removed


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
    result directory. Ordinary feedback returns to the static producer. When the
    existing feedback requests runtime disambiguation, the same repair slot instead
    invokes the configured dynamic role. Both paths rewrite the existing GT; neither
    adds a package artifact or schema field. A delta reviewer may confirm the repair
    direction, but only a new full reviewer can accept the resulting GT.
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
    runtime_role = str(review_stage.get("runtime_role") or "").strip()
    result_dir = Path(stage_kwargs["result_dir"])
    logs_dir = Path(stage_kwargs["logs_dir"])
    result = initial_result
    for round_number in range(1, rounds + 1):
        baseline_path = logs_dir / f"ground_truth.before_feedback_{round_number}.json"
        current_gt = result_dir / "ground_truth.json"
        if current_gt.exists():
            baseline_path.write_bytes(current_gt.read_bytes())

        runtime_requested = feedback_requests_runtime_disambiguation(result_dir)
        if runtime_requested and runtime_role:
            producer_retry = {
                **producer,
                "name": "02_runtime_disambiguation",
                "role": runtime_role,
                "_log_suffix": f"feedback_{round_number}",
            }
        else:
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


def feedback_requests_runtime_disambiguation(result_dir: Path) -> bool:
    """Whether the existing review feedback authorizes the conditional dynamic stage."""
    feedback_path = result_dir / "trace_feedback.json"
    if not feedback_path.is_file():
        return False
    try:
        feedback = load_json(feedback_path)
    except (json.JSONDecodeError, ValueError):
        # Reviewer output is model-authored. A malformed optional routing hint
        # must not abort the ordinary static repair loop.
        return False
    return bool(feedback.get("needs_runtime_disambiguation")) and bool(
        str(feedback.get("observe") or "").strip()
    )


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
        for index in range(min(len(before), len(after)), max(len(before), len(after))):
            changed.append(f"{path}[{index}]")
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
        errors="replace",
        capture_output=True,
        timeout=int(stage.get("timeout") or config.get("default_timeout") or 1800),
        env=stage_env(code_root, stage),
    )
    stdout_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(proc.stderr, encoding="utf-8", errors="replace")

    required_outputs_ok = check_required_outputs(stage, variables, started_ts)
    success_check_ok = (
        check_success(stage, variables)
        and check_validate_gt(stage, variables, repo_root, code_root)
        and check_assertion_stage_success(stage, variables)
        and check_runtime_disambiguation_success(stage, variables, started_ts)
    )
    return StageResult(
        name=name, command=command, returncode=proc.returncode, started_at=started_at,
        ended_at=now(), stdout_path=str(stdout_path), stderr_path=str(stderr_path),
        required_outputs_ok=required_outputs_ok, success_check_ok=success_check_ok,
        failure_kind=stage_failure_kind(
            name, proc.returncode, required_outputs_ok, success_check_ok
        ),
        duration_seconds=round(time.monotonic() - started_monotonic, 3),
    )


def stage_failure_kind(
    stage_name: str,
    returncode: int | None,
    required_outputs_ok: bool,
    success_check_ok: bool,
) -> str:
    if returncode == 0 and required_outputs_ok and success_check_ok:
        return ""
    if stage_name == ASSERTION_PLAN_STAGE:
        return (
            "assertion_plan_incomplete"
            if not required_outputs_ok
            else "assertion_plan_invalid"
        )
    if stage_name in {
        VULNERABLE_INSTRUMENTATION_STAGE,
        FIXED_INSTRUMENTATION_STAGE,
    }:
        return (
            "instrumentation_incomplete"
            if not required_outputs_ok
            else "instrumentation_invalid"
        )
    if stage_name == ASSERTION_EXECUTE_STAGE:
        return (
            "assertion_execution_incomplete"
            if not required_outputs_ok
            else "differential_unverified"
        )
    if stage_name == "04_reachability":
        return "reachability_failed"
    if stage_name == LEGACY_ASSERTION_STAGE:
        return "legacy_assertion_stage_failed"
    return "stage_command_failed" if returncode not in (None, 0) else "stage_gate_failed"


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
    proc = subprocess.run(
        cmd, cwd=repo_root, capture_output=True, text=True, errors="replace",
        env=stage_env(code_root, stage),
    )
    return proc.returncode == 0


def check_runtime_disambiguation_success(
    stage: dict[str, Any], variables: dict[str, str], started_ts: float
) -> bool:
    """Require a current apply/compile/run/reset cycle for the dynamic role."""
    if str(stage.get("name") or "") != "02_runtime_disambiguation":
        return True
    result_dir = Path(variables["result_dir"])
    state_path = result_dir / "arvo_workspace.json"
    if not state_path.is_file():
        return False
    try:
        state = load_json(state_path)
    except Exception:
        return False
    if (
        state.get("phase") != "vulnerable_source_reset"
        or state.get("vulnerable_compile_returncode") != 0
        or state.get("vulnerable_expectation_matched") is not True
    ):
        return False
    workspace = result_dir / "arvo_workspace"
    always_fresh = (
        workspace / "instrumentation_apply.log",
        workspace / "vulnerable_run.log",
        workspace / "reset_source.log",
    )
    if not all(
        path.is_file() and path.stat().st_mtime + 2 >= started_ts
        for path in always_fresh
    ):
        return False
    compile_logs = (
        workspace / "vulnerable_incremental_compile.log",
        workspace / "vulnerable_fallback_compile.log",
    )
    return any(
        path.is_file() and path.stat().st_mtime + 2 >= started_ts
        for path in compile_logs
    )


def check_assertion_stage_success(
    stage: dict[str, Any], variables: dict[str, str]
) -> bool:
    """Validate the frozen plan, then require fresh traces only at execution."""
    stage_name = str(stage.get("name") or "")
    if stage_name not in {
        ASSERTION_PLAN_STAGE,
        VULNERABLE_INSTRUMENTATION_STAGE,
        FIXED_INSTRUMENTATION_STAGE,
        ASSERTION_EXECUTE_STAGE,
        LEGACY_ASSERTION_STAGE,
    }:
        return True
    result_dir = Path(variables["result_dir"])
    report_path = result_dir / "assertion_preflight.json"
    try:
        report = load_json(report_path)
    except Exception:
        return False
    if report.get("ok") is not True:
        return False
    spec_path = result_dir / "candidate_assertions.json"
    if not spec_path.is_file():
        return False
    try:
        spec = load_json(spec_path)
    except Exception:
        return False
    if report.get("assertion_content_hash") != spec.get("content_hash"):
        return False
    marker_path = result_dir / ".assertion_spec_frozen.json"
    try:
        marker = load_json(marker_path)
    except Exception:
        return False
    if (
        marker.get("content_hash") != spec.get("content_hash")
        or marker.get("file_sha256") != (
            "sha256:" + hashlib.sha256(spec_path.read_bytes()).hexdigest()
        )
    ):
        return False
    expected_inputs = (
        "candidate_assertions.json",
        "candidate_invariants.json",
        "field_bindings.json",
        "event_locations.json",
    )
    input_hashes = report.get("input_hashes")
    if not isinstance(input_hashes, dict):
        return False
    for name in expected_inputs:
        path = result_dir / name
        if not path.is_file() or input_hashes.get(name) != (
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            return False
    if stage_name == ASSERTION_PLAN_STAGE:
        return True
    sample_id = str(spec.get("sample_id") or "")
    required_versions: tuple[str, ...] = ()
    if stage_name == VULNERABLE_INSTRUMENTATION_STAGE:
        required_versions = ("vulnerable",)
    elif stage_name == FIXED_INSTRUMENTATION_STAGE:
        required_versions = ("fixed",)
    elif stage_name == ASSERTION_EXECUTE_STAGE:
        required_versions = ("vulnerable", "fixed")
    if sample_id.startswith("arvo_") and stage_name != LEGACY_ASSERTION_STAGE:
        for version in required_versions:
            patch_path = result_dir / f"{version}-instrumentation.patch"
            build_report_path = (
                result_dir / f"{version}_instrumentation_preflight.json"
            )
            if not patch_path.is_file():
                return False
            patch_hash = (
                "sha256:" + hashlib.sha256(patch_path.read_bytes()).hexdigest()
            )
            try:
                build_report = load_json(build_report_path)
            except Exception:
                return False
            check = build_report.get("check")
            if (
                build_report.get("ok") is not True
                or build_report.get("version") != version
                or build_report.get("assertion_content_hash")
                != spec.get("content_hash")
                or not isinstance(check, dict)
                or check.get("patch_sha256") != patch_hash
                or check.get("apply_returncode") != 0
                or check.get("compile_returncode") != 0
            ):
                return False
    if stage_name in {
        VULNERABLE_INSTRUMENTATION_STAGE,
        FIXED_INSTRUMENTATION_STAGE,
    }:
        return True
    if sample_id.startswith("arvo_") and stage_name == LEGACY_ASSERTION_STAGE:
        build_report_path = result_dir / "instrumentation_build_preflight.json"
        try:
            build_report = load_json(build_report_path)
        except Exception:
            return False
        patch_hashes = {
            version: input_hashes.get(f"{version}-instrumentation.patch")
            for version in ("vulnerable", "fixed")
        }
        checks = build_report.get("checks")
        if (
            build_report.get("ok") is not True
            or build_report.get("assertion_content_hash") != spec.get("content_hash")
            or not isinstance(checks, dict)
            or any(
                not isinstance(checks.get(version), dict)
                or checks[version].get("patch_sha256") != patch_hashes[version]
                or checks[version].get("apply_returncode") != 0
                or checks[version].get("compile_returncode") != 0
                for version in ("vulnerable", "fixed")
            )
        ):
            return False
    verified_path = result_dir / "verified_invariants.json"
    if not verified_path.is_file():
        return False
    try:
        candidate_graph = load_json(result_dir / "candidate_invariants.json")
        verified_graph = load_json(verified_path)
    except Exception:
        return False
    if not verified_graph_is_candidate_subset(candidate_graph, verified_graph):
        return False
    preflight_mtime = report_path.stat().st_mtime
    traces = (
        result_dir / "vulnerable_assertion_trace.txt",
        result_dir / "fixed_assertion_trace.txt",
    )
    return all(path.is_file() and path.stat().st_mtime >= preflight_mtime for path in traces)


def check_assertion_preflight_success(
    stage: dict[str, Any], variables: dict[str, str]
) -> bool:
    """Backward-compatible name used by integrations and older tests."""
    return check_assertion_stage_success(stage, variables)


def verified_graph_is_candidate_subset(
    candidate: dict[str, Any], verified: dict[str, Any]
) -> bool:
    """Runtime may remove invariants or add evidence, but not rewrite the plan."""
    def entries(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        criterion = document.get("root_cause_criterion")
        if isinstance(criterion, dict) and criterion.get("invariant_id"):
            result[str(criterion["invariant_id"])] = criterion
        for node in document.get("nodes", []):
            if isinstance(node, dict) and node.get("invariant_id"):
                result[str(node["invariant_id"])] = node
        for edge in document.get("edges", []):
            if isinstance(edge, dict) and edge.get("invariant_id"):
                result[str(edge["invariant_id"])] = edge
        return result

    def preserves_candidate_fields(
        candidate_value: Any, verified_value: Any
    ) -> bool:
        if isinstance(candidate_value, dict):
            return isinstance(verified_value, dict) and all(
                key in verified_value
                and preserves_candidate_fields(value, verified_value[key])
                for key, value in candidate_value.items()
            )
        if isinstance(candidate_value, list):
            return candidate_value == verified_value
        return candidate_value == verified_value

    candidate_entries = entries(candidate)
    verified_entries = entries(verified)
    return bool(verified_entries) and all(
        invariant_id in candidate_entries
        and preserves_candidate_fields(
            candidate_entries[invariant_id], verified_entry
        )
        for invariant_id, verified_entry in verified_entries.items()
    )


def get_nested(data: dict[str, Any], field: str) -> Any:
    current: Any = data
    for part in field.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def make_stage_filter(stages: list[dict[str, Any]], start_at: str, stop_after: str, only: str):
    names = [str(stage.get("name") or "") for stage in stages]
    start_at = resolve_stage_alias(start_at, names)
    stop_after = resolve_stage_alias(stop_after, names)
    only = resolve_stage_alias(only, names)
    if only:
        return lambda name: name == only
    start_index = names.index(start_at) if start_at in names else 0
    stop_index = names.index(stop_after) if stop_after in names else len(names) - 1
    return lambda name: start_index <= names.index(name) <= stop_index


def needs_resume_source_hydration(
    active_stages: list[dict[str, Any]], dry_run: bool = False
) -> bool:
    """Whether selected work skips prepare but invokes an agent that needs source."""
    if dry_run or not active_stages:
        return False
    if any(str(stage.get("name") or "") == "00_prepare" for stage in active_stages):
        return False
    return any(bool(stage.get("role")) for stage in active_stages)


def runtime_disambiguation_enabled(
    config: dict[str, Any], command_line_enabled: bool = False
) -> bool:
    """Resolve the default-OFF workflow gate with a per-invocation opt-in."""
    return command_line_enabled or bool(config.get("runtime_disambiguation", False))


def validate_stage_bounds(
    stages: list[dict[str, Any]], start_at: str, stop_after: str, only: str
) -> None:
    names = [str(stage.get("name") or "") for stage in stages]
    start_at = resolve_stage_alias(start_at, names)
    stop_after = resolve_stage_alias(stop_after, names)
    only = resolve_stage_alias(only, names)
    for label, value in (
        ("--start-at", start_at),
        ("--stop-after", stop_after),
        ("--only", only),
    ):
        if value and value not in names:
            raise SystemExit(f"{label} names unknown stage {value!r}")
    if only and (start_at or stop_after):
        raise SystemExit("--only cannot be combined with --start-at or --stop-after")
    if start_at and stop_after and names.index(start_at) > names.index(stop_after):
        raise SystemExit("--start-at must not follow --stop-after")


def resolve_stage_alias(name: str, available: list[str]) -> str:
    if not name or name in available:
        return name
    resolved = STAGE_ALIASES.get(name, name)
    return resolved if resolved in available else name


def should_stage_repair(
    result_dir: Path, start_at: str, only: str, dry_run: bool
) -> bool:
    """Existing evidence bundles are never edited in place by a partial workflow."""
    if dry_run or not (start_at or only):
        return False
    durable = (
        "ground_truth.json",
        "verified_assertions.json",
        "verified_invariants.json",
        "assertion_results.json",
    )
    return all((result_dir / name).is_file() for name in durable)


def repair_staging_dir(result_dir: Path) -> Path:
    return result_dir.with_name(result_dir.name + ".repair-staging")


def prepare_repair_staging(source: Path, staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)
    copied = subprocess.run(
        ["cp", "--reflink=auto", "-a", str(source), str(staging)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if copied.returncode != 0:
        shutil.copytree(source, staging, symlinks=True)
    for name in (".gt_generation.lock", "gt_generation_state.json"):
        path = staging / name
        if path.is_file() or path.is_symlink():
            path.unlink()


def prepare_transactional_repair(
    source: Path, staging: Path, *, reuse: bool = False
) -> None:
    """Create a fresh staging copy unless an explicit continuation reuses it."""
    if reuse:
        if not staging.is_dir():
            raise SystemExit(
                f"Cannot reuse missing repair staging directory: {staging}"
            )
        return
    prepare_repair_staging(source, staging)
    write_repair_context(source, staging)


def write_repair_context(source: Path, staging: Path) -> dict[str, Any]:
    """Expose prior verified measurements without treating them as new proof."""
    context: dict[str, Any] = {
        "schema_version": "gt-repair-context-v1",
        "source_result_dir": str(source),
        "generated_at": now(),
        "prior_package_audit_ok": False,
        "prior_evidence": {},
    }
    try:
        try:
            from gt_toolkit.package_audit import audit_package
        except ModuleNotFoundError:
            from gt_generation.gt_toolkit.package_audit import audit_package

        audit = audit_package(source)
        context["prior_package_audit_ok"] = bool(audit["ok"])
        context["prior_package_audit_errors"] = audit["errors"]
    except Exception as exc:
        context["prior_package_audit_errors"] = [str(exc)]
    for name in (
        "verified_assertions.json",
        "verified_invariants.json",
        "assertion_results.json",
        "field_bindings.json",
        "event_locations.json",
    ):
        path = source / name
        if path.is_file():
            try:
                context["prior_evidence"][name] = load_json(path)
            except Exception:
                context["prior_evidence"][name] = {"unreadable": True}
    write_json(staging / "repair_context.json", context)
    return context


def install_generation_provenance(result_dir: Path) -> None:
    source_text = os.environ.get("GT_GENERATION_PROVENANCE_SOURCE", "").strip()
    if not source_text:
        return
    source = Path(source_text)
    if source.is_file() and source.resolve() != (
        result_dir / "generation_provenance.json"
    ).resolve():
        shutil.copy2(source, result_dir / "generation_provenance.json")


def repair_package_ready_to_publish(result_dir: Path) -> bool:
    """Never trust a configurable Stage-05 command as the sole publish gate."""
    if not (result_dir / "evidence_commitment.json").is_file():
        return False
    try:
        try:
            from gt_toolkit.package_audit import audit_package
        except ModuleNotFoundError:
            from gt_generation.gt_toolkit.package_audit import audit_package

        report = audit_package(result_dir)
        return bool(report["ok"]) and not report["warnings"]
    except Exception:
        return False


def publish_repair_staging(staging: Path, published: Path) -> None:
    """Replace a completed package only after the staged package passes Stage 05."""
    backup = published.with_name(published.name + ".repair-backup")
    if backup.exists():
        shutil.rmtree(backup)
    os.replace(published, backup)
    try:
        os.replace(staging, published)
    except BaseException:
        os.replace(backup, published)
        raise
    shutil.rmtree(backup)


def clear_stale_feedback_control_files(logs_dir: Path) -> list[Path]:
    """Remove prior-run manifests that must never enter a new review loop.

    These files are transient coordination state generated from the current
    producer output. Keeping them across runner invocations lets an initial
    Stage 03 reviewer mistake a previous run's round-2 delta for current
    evidence. Human-readable role stdout/stderr and review artifacts remain.
    """
    removed: list[Path] = []
    for pattern in (
        "ground_truth.before_feedback_*.json",
        "ground_truth.delta_feedback_*.json",
    ):
        for path in logs_dir.glob(pattern):
            if path.is_file():
                path.unlink()
                removed.append(path)
    return removed


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
