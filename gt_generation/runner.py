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
import re
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
REPRODUCER_STAGE = "01_reproducer"
ASSERTION_PLAN_STAGE = "04_assertion_plan"
VULNERABLE_INSTRUMENTATION_STAGE = "04_instrument_vulnerable"
FIXED_INSTRUMENTATION_STAGE = "04_instrument_fixed"
ASSERTION_EXECUTE_STAGE = "04_assertion_execute"
LEGACY_ASSERTION_STAGE = "04_assertion_validator"
ASSERTION_SEMANTIC_REPAIR_STAGES = (
    ASSERTION_PLAN_STAGE,
    VULNERABLE_INSTRUMENTATION_STAGE,
    FIXED_INSTRUMENTATION_STAGE,
    ASSERTION_EXECUTE_STAGE,
)
ASSERTION_PLAN_INPUTS = (
    "candidate_assertions.json",
    "candidate_invariants.json",
    "field_bindings.json",
    "event_locations.json",
    ".assertion_spec_frozen.json",
    "assertion_preflight.json",
)
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
    resumable_ok = current_resumable_ok_stages(
        stages=stages,
        prior_ok=prior_ok,
        config=config,
        sample=sample,
        sample_path=args.sample.resolve(),
        sample_id=sample_id,
        repo_root=repo_root,
        code_root=code_root,
        result_dir=result_dir,
    )

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
        and str(stage.get("name") or "") not in resumable_ok
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
        if not should_run(name) or name in resumable_ok:
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
        if (
            not result.ok
            and name == ASSERTION_EXECUTE_STAGE
            and not args.dry_run
        ):
            if result.failure_kind == "differential_unverified":
                result = run_assertion_semantic_repair_loop(
                    initial_result=result,
                    stages=stages,
                    state_path=state_path,
                    stage_kwargs=stage_kwargs,
                )
            else:
                write_stage_retry_feedback(name, result_dir, result=result)
        if (
            not result.ok
            and name in {VULNERABLE_INSTRUMENTATION_STAGE, FIXED_INSTRUMENTATION_STAGE}
            and not args.dry_run
        ):
            write_stage_retry_feedback(name, result_dir)
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
    should_compact_result = (
        not failed
        and config.get("compact_on_success")
        and not args.dry_run
        and should_run(FINAL_STAGE)
        and FINAL_STAGE not in prior_ok
    )
    if should_compact_result:
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
    while (
        not result.ok
        and attempt <= retries
        and not (result.dry_run or result.skipped)
        and stage_retry_is_useful(str(stage.get("name") or ""), result.failure_kind)
    ):
        write_stage_retry_feedback(
            str(stage.get("name") or ""),
            Path(kwargs["result_dir"]),
            result=result,
        )
        attempt += 1
        prepare_stage_retry(
            str(stage.get("name") or ""),
            Path(kwargs["result_dir"]),
        )
        result = run_stage(**kwargs)
        result.attempts = attempt
    return result


def stage_retry_is_useful(stage_name: str, failure_kind: str) -> bool:
    """Avoid replaying frozen work after deterministic evidence disproves it."""
    if (
        stage_name == ASSERTION_EXECUTE_STAGE
        and failure_kind == "differential_unverified"
    ):
        return False
    return True


def run_assertion_semantic_repair_loop(
    *,
    initial_result: StageResult,
    stages: list[dict[str, Any]],
    state_path: Path,
    stage_kwargs: dict[str, Any],
) -> StageResult:
    """Retry Stage 04 from planning when runtime disproves the root predicate.

    A failed required assertion is a semantic failure of the frozen Stage 04A
    contract. Re-running only Stage 04B repeats the same invalid predicate, so
    route deterministic differential failures back through the whole Stage 04
    chain with the generated plan feedback visible to the planner.
    """
    if initial_result.failure_kind != "differential_unverified":
        return initial_result
    result_dir = Path(stage_kwargs["result_dir"])
    write_assertion_plan_feedback(result_dir)
    stage_by_name = {str(stage.get("name") or ""): stage for stage in stages}
    semantic_stages = [
        stage_by_name[name]
        for name in ASSERTION_SEMANTIC_REPAIR_STAGES
        if name in stage_by_name
    ]
    if len(semantic_stages) != len(ASSERTION_SEMANTIC_REPAIR_STAGES):
        return initial_result
    config = stage_kwargs.get("config")
    rounds = 1
    if isinstance(config, dict):
        rounds = int(config.get("assertion_semantic_feedback_rounds", rounds))
    result = initial_result
    for round_number in range(1, max(0, rounds) + 1):
        for semantic_stage in semantic_stages:
            retry_stage = {
                **semantic_stage,
                "_log_suffix": f"semantic_feedback_{round_number}",
            }
            prepare_stage_entry(str(retry_stage.get("name") or ""), result_dir)
            result = run_stage_with_retries(
                **{**stage_kwargs, "stage": retry_stage}
            )
            append_stage(state_path, result)
            if not result.ok:
                if result.name == ASSERTION_EXECUTE_STAGE:
                    write_stage_retry_feedback(result.name, result_dir, result=result)
                elif result.name in {
                    VULNERABLE_INSTRUMENTATION_STAGE,
                    FIXED_INSTRUMENTATION_STAGE,
                }:
                    write_stage_retry_feedback(result.name, result_dir)
                break
        if result.ok:
            return result
        if result.name != ASSERTION_EXECUTE_STAGE:
            return result
        if result.failure_kind != "differential_unverified":
            return result
        write_assertion_plan_feedback(result_dir)
    return result


def write_assertion_plan_feedback(result_dir: Path) -> Path | None:
    """Summarize a failed Stage 04B run as actionable input for the next 04A.

    Stage 04B is not allowed to edit the frozen assertion plan. When execution
    proves the plan wrong, persist a compact diagnosis where Stage 04A already
    looks for it, so a retry from 04A can rewrite the root obligation instead of
    repeating the same frozen predicate.
    """
    results_path = result_dir / "assertion_results.json"
    if not results_path.is_file():
        return None
    try:
        results = load_json(results_path)
    except Exception:
        return None

    sample_id = str(results.get("sample_id") or result_dir.name)
    lines = [
        f"# Assertion Plan Feedback for {sample_id}",
        "",
        "Stage 04B could not verify the frozen assertion plan. The next Stage 04A run must rewrite the semantic assertion plan; do not reuse the failed predicate, event placement, or instrumentation expression unchanged.",
        "",
    ]

    failure_class = results.get("failure_class")
    if failure_class:
        lines.extend(["## Failure Class", "", str(failure_class), ""])
    summary = results.get("summary")
    if summary:
        lines.extend(["## Summary", "", str(summary), ""])

    stage04b_failure = results.get("stage04b_failure")
    if isinstance(stage04b_failure, dict):
        lines.extend(["## Stage 04B Diagnosis", ""])
        for key in ("classification", "message"):
            value = stage04b_failure.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        evidence = stage04b_failure.get("evidence")
        if evidence:
            lines.append(f"- evidence: {json.dumps(evidence, ensure_ascii=False, sort_keys=True)}")
        lines.append("")

    runs = results.get("runs")
    if isinstance(runs, dict):
        lines.extend(["## Runtime/Build Runs", ""])
        for side in ("vulnerable", "fixed"):
            run = runs.get(side)
            if not isinstance(run, dict):
                continue
            parts = [
                f"runner={run.get('runner_result')}",
                f"returncode={run.get('returncode')}",
            ]
            if run.get("compile_failed") is not None:
                parts.append(f"compile_failed={run.get('compile_failed')}")
            if run.get("failure_summary"):
                parts.append(f"failure_summary={run.get('failure_summary')}")
            lines.append(f"- {side}: " + "; ".join(parts))
        lines.append("")

    assertion_items: list[dict[str, Any]] = []
    for key in (
        "assertions",
        "required_assertions",
        "observed_assertions",
        "transition_assertions",
    ):
        value = results.get(key)
        if isinstance(value, list):
            assertion_items.extend(item for item in value if isinstance(item, dict))
    seen_ids: set[str] = set()
    failing_items: list[dict[str, Any]] = []
    for item in assertion_items:
        item_id = str(item.get("id") or "")
        dedupe = item_id or json.dumps(item, sort_keys=True, default=str)
        if dedupe in seen_ids:
            continue
        seen_ids.add(dedupe)
        if (
            item.get("verified") is False
            or item.get("differential") in {"failed", "unavailable"}
            or item.get("status") in {"not_exercised", "failed"}
            or item.get("verification_error")
            or item.get("probe_placement_error")
        ):
            failing_items.append(item)

    if failing_items:
        lines.extend(["## Failed Assertions", ""])
        for item in failing_items:
            lines.append(f"### {item.get('id', '<unknown>')}")
            for key in (
                "kind",
                "differential",
                "status",
                "verification_error",
                "probe_placement_error",
            ):
                value = item.get(key)
                if value:
                    lines.append(f"- {key}: {value}")
            matrix = item.get("matrix")
            if isinstance(matrix, dict):
                for side in ("vulnerable", "fixed"):
                    original = (matrix.get(side) or {}).get("original")
                    if isinstance(original, dict):
                        compact = {
                            key: original.get(key)
                            for key in (
                                "status",
                                "satisfied",
                                "triggered",
                                "left",
                                "op",
                                "right",
                                "from",
                                "to",
                                "ordered",
                            )
                            if key in original
                        }
                        lines.append(
                            f"- {side} original: "
                            f"{json.dumps(compact, ensure_ascii=False, sort_keys=True)}"
                        )
            lines.append("")

    lines.extend([
        "## Required Stage 04A Repair",
        "",
        "1. Re-read `ground_truth.json`, `sanitizer_trace.txt`, `reproduction_report.json`, and the vulnerable source.",
        "2. Redesign the `required` root obligation so vulnerable original violates it when the protected operation runs.",
        "3. Ensure the fixed original satisfies the same obligation or avoids the protected operation through a real guard.",
        "4. Move any protected event to immediately before the dangerous operation and after every guard that can skip it.",
        "5. Avoid instrumentation expressions that are known to fail compilation; bind source variables or simple runtime fields instead.",
        "6. Regenerate `candidate_assertions.json`, `candidate_invariants.json`, `field_bindings.json`, `event_locations.json`, `.assertion_spec_frozen.json`, and `assertion_preflight.json` from scratch.",
        "",
    ])

    out = result_dir / "assertion_plan_feedback.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_stage_retry_feedback(
    stage_name: str,
    result_dir: Path,
    *,
    result: StageResult | None = None,
) -> Path | None:
    """Write deterministic repair hints before retrying or stopping a stage."""
    if stage_name == REPRODUCER_STAGE:
        required = (
            "sanitizer_trace.txt",
            "sample_state.json",
            "reproduction_report.json",
        )
        missing = [name for name in required if not (result_dir / name).is_file()]
        lines = [
            "# Stage 01 retry feedback",
            "",
            "The previous attempt did not complete the Stage 01 contract.",
            "Continue from any existing checkout, build artifact, and logs in this result directory; do not restart successful work unnecessarily.",
            "You must still verify the exact fixed commit with the same PoC and write all three fresh official outputs before returning.",
            "Candidate-prefixed runtime files are hints only; do not rename them as proof without executing both sides.",
            "",
            "Missing required outputs: " + (", ".join(missing) if missing else "none (rewrite stale or invalid outputs)"),
            "Prior failure kind: " + (result.failure_kind if result is not None else "unknown"),
            "",
        ]
        out = result_dir / "stage01_retry_feedback.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        return out
    if stage_name == VULNERABLE_INSTRUMENTATION_STAGE:
        return write_instrumentation_feedback(result_dir, "vulnerable")
    if stage_name == FIXED_INSTRUMENTATION_STAGE:
        return write_instrumentation_feedback(result_dir, "fixed")
    if stage_name == ASSERTION_EXECUTE_STAGE:
        if result is not None and result.failure_kind == "differential_unverified":
            return write_assertion_plan_feedback(result_dir)
        return write_assertion_execute_feedback(result_dir)
    return None


def assertion_execute_needs_execution_retry(result_dir: Path) -> bool:
    """Return true when 04B failed from missing execution evidence, not semantics.

    A guarded fixed-side witness is acceptable only after Stage 04B records one
    fixed non-original perturbation case. If the deterministic projection says
    that perturbation was needed but not attempted, the plan may still be good:
    the execute stage simply stopped before collecting all evidence.
    """
    results_path = result_dir / "assertion_results.json"
    if not results_path.is_file():
        return False
    try:
        results = load_json(results_path)
    except Exception:
        return False
    missing_fixed_perturbation = False
    original = str(results.get("original_case") or "original")
    for item in results.get("assertions", []):
        if not isinstance(item, dict) or item.get("kind") != "required":
            continue
        matrix = item.get("matrix") if isinstance(item.get("matrix"), dict) else {}
        vulnerable_original = (
            matrix.get("vulnerable", {}).get(original, {})
            if isinstance(matrix.get("vulnerable"), dict)
            else {}
        )
        fixed_original = (
            matrix.get("fixed", {}).get(original, {})
            if isinstance(matrix.get("fixed"), dict)
            else {}
        )
        if vulnerable_original.get("status") != "violated":
            continue
        error = str(item.get("verification_error") or "")
        if (
            fixed_original.get("status") in {"guarded", "avoided"}
            and "add exactly one perturbation case" in error
        ):
            missing_fixed_perturbation = True
    if not missing_fixed_perturbation:
        return False
    perturbation_path = result_dir / "perturbation_results.json"
    if not perturbation_path.is_file():
        return True
    try:
        perturbation = load_json(perturbation_path)
    except Exception:
        return True
    return (
        perturbation.get("needed") is True
        and perturbation.get("single_perturbation_attempt_recorded") is not True
    )


def assertion_execute_has_trace_format_error(result_dir: Path) -> bool:
    """Return true when deterministic projection rejected malformed raw traces."""
    stderr_path = result_dir / "role_logs" / f"{ASSERTION_EXECUTE_STAGE}.finalize.stderr.txt"
    if not stderr_path.is_file():
        return False
    text = stderr_path.read_text(encoding="utf-8", errors="replace")
    return (
        "malformed CASE line" in text
        or "CASE name=<name> rc=<int> result=<result>" in text
        or "parse_trace_matrix" in text
    )


def append_assertion_execute_blockers(lines: list[str], result_dir: Path) -> None:
    """Add deterministic 04B blockers that do not require a semantic rewrite."""
    results_path = result_dir / "assertion_results.json"
    if not results_path.is_file():
        return
    try:
        results = load_json(results_path)
    except Exception:
        return
    blockers: list[dict[str, Any]] = []
    for item in results.get("assertions", []):
        if not isinstance(item, dict) or item.get("kind") != "required":
            continue
        error = str(item.get("verification_error") or "")
        if "add exactly one perturbation case" not in error:
            continue
        matrix = item.get("matrix") if isinstance(item.get("matrix"), dict) else {}
        fixed_original = (
            matrix.get("fixed", {}).get("original", {})
            if isinstance(matrix.get("fixed"), dict)
            else {}
        )
        blockers.append({
            "id": item.get("id"),
            "fixed_status": fixed_original.get("status"),
            "verification_error": error,
        })
    if not blockers:
        return
    lines.extend([
        "",
        "## Execution Blockers From Deterministic Projection",
        "",
    ])
    for blocker in blockers:
        lines.append(
            "- "
            + json.dumps(blocker, ensure_ascii=False, sort_keys=True)
        )


def write_assertion_execute_feedback(result_dir: Path) -> Path | None:
    """Summarize missing Stage 04B outputs for an execution-only retry.

    Missing raw traces or deterministic JSON projections are an execution
    completeness issue, not evidence that Stage 04A chose the wrong predicate.
    Keep this feedback separate from assertion_plan_feedback.md so the next
    04B attempt continues from the frozen plan instead of needlessly rewriting
    semantics.
    """
    required = (
        "vulnerable_assertion_trace.txt",
        "fixed_assertion_trace.txt",
        "assertion_results.json",
        "perturbation_results.json",
        "verified_assertions.json",
        "verified_invariants.json",
    )
    missing = [name for name in required if not (result_dir / name).is_file()]
    stale_or_empty = [
        name for name in required
        if (result_dir / name).is_file() and (result_dir / name).stat().st_size == 0
    ]
    lines = [
        f"# Assertion Execute Feedback for {result_dir.name}",
        "",
        "Stage 04B did not produce a complete execution package. This is an execution completeness retry, not a Stage 04A semantic rewrite request.",
        "",
        "## Missing Or Empty Outputs",
        "",
    ]
    if missing:
        lines.append("- missing: " + ", ".join(missing))
    if stale_or_empty:
        lines.append("- empty: " + ", ".join(stale_or_empty))
    if not missing and not stale_or_empty:
        lines.append("- none detected by file presence; inspect finalizer logs below.")
    integrity_path = result_dir / "assertion_execute_integrity.json"
    if integrity_path.is_file():
        try:
            integrity = load_json(integrity_path)
        except Exception:
            integrity = {}
        lines.extend([
            "",
            "## Frozen Plan Mutation",
            "",
            "Stage 04B modified one or more frozen Stage 04A files. The runner restored the original bytes and rejected the execution attempt.",
        ])
        modified = integrity.get("modified_files")
        if isinstance(modified, list):
            for item in modified:
                if isinstance(item, dict):
                    lines.append(
                        "- "
                        + json.dumps(item, ensure_ascii=False, sort_keys=True)
                    )
    if assertion_execute_needs_execution_retry(result_dir):
        lines.extend([
            "",
            "## Missing Fixed Perturbation",
            "",
            "The deterministic assertion projection shows the fixed original skipped the protected event and a single non-original fixed perturbation case was required but not recorded. This is still a Stage 04B execution issue, not a Stage 04A semantic rewrite.",
            "",
            "Run exactly one closest source-grounded fixed-side perturbation through `gt_toolkit repo-workspace run --version fixed --append-trace --case-name <name> --poc <result-dir-local-poc>` so the fixed trace contains a normal non-original `CASE name=... rc=... result=...` block. Do not edit the trace by hand.",
        ])
    if assertion_execute_has_trace_format_error(result_dir):
        lines.extend([
            "",
            "## Malformed Raw Trace",
            "",
            "The deterministic assertion finalizer rejected the raw trace syntax. Rebuild the affected trace only through the deterministic workspace runner. Do not hand-write, replace, or post-process `CASE`/`ENDCASE` framing.",
        ])
    append_assertion_execute_blockers(lines, result_dir)
    lines.extend([
        "",
        "## Required 04B Repair",
        "",
        "1. Reuse the frozen `candidate_assertions.json`, `candidate_invariants.json`, `field_bindings.json`, `event_locations.json`, `.assertion_spec_frozen.json`, and instrumentation patches.",
        "2. Execute vulnerable and fixed sides serially through the deterministic workspace runner.",
        "3. Do not stop after the vulnerable side. The fixed trace is mandatory before any JSON projection can be valid.",
        "4. Do not hand-write or post-process trace files; the workspace runner must produce normal CASE/ENDCASE framing.",
        "5. After both raw traces exist, run the deterministic `gt_toolkit assertions` projection to produce `assertion_results.json`, `perturbation_results.json`, and `verified_assertions.json`.",
        "",
    ])
    append_log_tail(
        lines,
        "Stage 04B Stdout",
        result_dir / "role_logs" / f"{ASSERTION_EXECUTE_STAGE}.stdout.txt",
    )
    append_log_tail(
        lines,
        "Stage 04B Stderr",
        result_dir / "role_logs" / f"{ASSERTION_EXECUTE_STAGE}.stderr.txt",
    )
    append_log_tail(
        lines,
        "Finalizer Stderr",
        result_dir / "role_logs" / f"{ASSERTION_EXECUTE_STAGE}.finalize.stderr.txt",
    )
    out = result_dir / "assertion_execute_feedback.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_instrumentation_feedback(result_dir: Path, version: str) -> Path | None:
    """Expose repo/arvo preflight failures as concrete patch-repair input.

    The instrumentation agent can repair an apply/compile mismatch only if it
    sees the exact deterministic gate output. Persisting this feedback also
    makes interrupted repair queues auditable without asking a human to inspect
    several tool-specific logs first.
    """
    report_path = result_dir / f"{version}_instrumentation_preflight.json"
    if not report_path.is_file():
        return None
    try:
        report = load_json(report_path)
    except Exception:
        return None
    if report.get("ok") is True:
        return None
    track = str(report.get("track") or "")
    prefix = "repo_workspace" if track.startswith("repo/") else "arvo_workspace"
    log_dir = result_dir / prefix
    apply_log = log_dir / f"plan_{version}_apply.log"
    compile_log = log_dir / f"plan_{version}_compile.log"

    check = report.get("check") if isinstance(report.get("check"), dict) else {}
    lines = [
        f"# Instrumentation Feedback for {result_dir.name} ({version})",
        "",
        "The deterministic instrumentation preflight rejected the current patch. The next instrumentation attempt must repair only the observation patch; do not rewrite the frozen assertion plan, invariant graph, field bindings, event locations, fine trace, or ground truth.",
        "",
        "## Preflight Summary",
        "",
        f"- track: {track or '<unknown>'}",
        f"- patch: {check.get('patch') or f'{version}-instrumentation.patch'}",
        f"- apply_returncode: {check.get('apply_returncode')}",
        f"- compile_returncode: {check.get('compile_returncode')}",
        f"- setup_masks_failures: {check.get('setup_masks_failures')}",
    ]
    markers = check.get("compile_failure_markers")
    if markers:
        lines.append(
            "- compile_failure_markers: "
            + json.dumps(markers, ensure_ascii=False, sort_keys=True)
        )
    runtime_field_quality = check.get("runtime_field_quality")
    if isinstance(runtime_field_quality, dict):
        errors = runtime_field_quality.get("errors")
        if errors:
            lines.append(
                "- runtime_field_quality_errors: "
                + json.dumps(errors, ensure_ascii=False, sort_keys=True)
            )
    lines.extend([
        "",
        "## Required Repair",
        "",
        "1. Re-read `candidate_assertions.json`, `field_bindings.json`, `event_locations.json`, and the real source selected by the preflight gate.",
        "2. If `apply_returncode` is non-zero, regenerate the patch against the exact commit/tree used by the gate, not a previously patched or fixed checkout.",
        "3. If `compile_returncode` is non-zero, fix only C/C++ syntax, includes, scope, or expression availability in the observation patch.",
        "4. If `runtime_field_quality_errors` is present, rewrite the patch so every non-literal field used by a required assertion is computed from real program state at the event. Literal fields such as false_literal/null_literal may be constants; measured fields such as len, alive, initialized, or free_before_use must not be printed as the expected answer.",
        "5. Rerun the same preflight command until this side's report has `ok: true`.",
        "",
    ])
    append_log_tail(lines, "Apply Log", apply_log)
    append_log_tail(lines, "Compile Log", compile_log)
    out = result_dir / f"instrumentation_feedback_{version}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def append_log_tail(lines: list[str], title: str, path: Path, *, max_lines: int = 80) -> None:
    lines.extend([f"## {title}", ""])
    if not path.is_file():
        lines.extend([f"Missing log: `{path}`", ""])
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    tail = text.splitlines()[-max_lines:]
    lines.extend(["```text", *tail, "```", ""])


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
            "assertion_execute_integrity.json",
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
        if stage_name == ASSERTION_EXECUTE_STAGE:
            workspace = result_dir / "repo_workspace"
            for pattern in (
                "vulnerable_*_run.log",
                "vulnerable_*_run.json",
                "fixed_*_run.log",
                "fixed_*_run.json",
            ):
                for path in workspace.glob(pattern):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                        removed.append(path)
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
    frozen_inputs = (
        snapshot_assertion_plan_inputs(result_dir)
        if name == ASSERTION_EXECUTE_STAGE and not dry_run
        else {}
    )

    if dry_run:
        stdout_path.write_text(command + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return StageResult(
            name=name, command=command, returncode=None, started_at=started_at,
            ended_at=now(), stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            required_outputs_ok=True, success_check_ok=True, dry_run=True,
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
        )

    with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout_stream, \
            stderr_path.open("w", encoding="utf-8", errors="replace") as stderr_stream:
        proc = subprocess.run(
            command,
            cwd=repo_root,
            shell=True,
            text=True,
            errors="replace",
            stdout=stdout_stream,
            stderr=stderr_stream,
            timeout=int(stage.get("timeout") or config.get("default_timeout") or 1800),
            env=stage_env(code_root, stage),
        )

    returncode = proc.returncode
    if name == REPRODUCER_STAGE and not dry_run and proc.returncode == 0:
        portability_code = finalize_stage01_portability(
            result_dir=result_dir,
            logs_dir=logs_dir,
            timeout=int(stage.get("portability_timeout") or 7200),
        )
        if portability_code != 0:
            returncode = portability_code or returncode
    if name == ASSERTION_EXECUTE_STAGE:
        mutation_report = restore_modified_assertion_plan_inputs(
            result_dir, frozen_inputs
        )
        if mutation_report["modified_files"]:
            write_json(
                result_dir / "assertion_execute_integrity.json",
                mutation_report,
            )
            returncode = 3
        else:
            integrity_path = result_dir / "assertion_execute_integrity.json"
            if integrity_path.is_file() or integrity_path.is_symlink():
                integrity_path.unlink()
        if returncode != 3:
            finalize_code = finalize_assertion_execute_outputs(
                variables=variables,
                repo_root=repo_root,
                code_root=code_root,
                result_dir=result_dir,
                logs_dir=logs_dir,
            )
            # Stage 04B's agent owns execution and raw trace collection. The runner
            # owns the frozen assertion contract: final JSON artifacts must be a
            # deterministic projection of the raw traces, never a hand-authored
            # interpretation left by the agent. If the deterministic finalizer
            # succeeds, allow a non-zero agent exit caused by its own stale gate
            # interpretation to be recovered. If it fails, the stage fails.
            returncode = 0 if finalize_code == 0 else (finalize_code or proc.returncode)

    required_outputs_ok = check_required_outputs(stage, variables, started_ts)
    success_check_ok = (
        check_success(stage, variables)
        and check_validate_gt(stage, variables, repo_root, code_root)
        and check_assertion_stage_success(stage, variables)
        and check_runtime_disambiguation_success(stage, variables, started_ts)
    )
    return StageResult(
        name=name, command=command, returncode=returncode, started_at=started_at,
        ended_at=now(), stdout_path=str(stdout_path), stderr_path=str(stderr_path),
        required_outputs_ok=required_outputs_ok, success_check_ok=success_check_ok,
        failure_kind=stage_failure_kind(
            name, returncode, required_outputs_ok, success_check_ok, result_dir
        ),
        duration_seconds=round(time.monotonic() - started_monotonic, 3),
    )


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def finalize_stage01_portability(
    *, result_dir: Path, logs_dir: Path, timeout: int
) -> int:
    """Freeze and independently replay Stage 01's non-ARVO runtime contract."""
    if result_dir.name.startswith("arvo_"):
        return 0

    try:
        from gt_toolkit.portability import materialize_stage01_portability
    except ImportError:
        from gt_generation.gt_toolkit.portability import (
            materialize_stage01_portability,
        )

    stdout_path = logs_dir / f"{REPRODUCER_STAGE}.portability.stdout.txt"
    stderr_path = logs_dir / f"{REPRODUCER_STAGE}.portability.stderr.txt"
    try:
        report = materialize_stage01_portability(result_dir, timeout=timeout)
        stdout_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        stderr_path.write_text("", encoding="utf-8")
        return 0 if report.get("runtime_portable") is True else 1
    except Exception as exc:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(
            f"Stage 01 portability gate failed: {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        return 1


def snapshot_assertion_plan_inputs(result_dir: Path) -> dict[str, dict[str, Any]]:
    """Capture frozen Stage 04A files before Stage 04B runs.

    The execute agent may create raw traces, but the assertion contract was
    frozen by Stage 04A. Keep bytes in memory so an accidental 04B rewrite cannot
    contaminate deterministic projection or future retries.
    """
    snapshot: dict[str, dict[str, Any]] = {}
    for name in ASSERTION_PLAN_INPUTS:
        path = result_dir / name
        if not path.is_file():
            snapshot[name] = {"exists": False, "sha256": "", "bytes": b""}
            continue
        raw = path.read_bytes()
        snapshot[name] = {
            "exists": True,
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "bytes": raw,
        }
    return snapshot


def restore_modified_assertion_plan_inputs(
    result_dir: Path, snapshot: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Restore frozen inputs if Stage 04B modified them and report the violation."""
    modified: list[dict[str, Any]] = []
    for name, info in snapshot.items():
        path = result_dir / name
        before_exists = bool(info.get("exists"))
        before_hash = str(info.get("sha256") or "")
        after_exists = path.is_file()
        after_hash = sha256_file(path) if after_exists else ""
        if before_exists == after_exists and before_hash == after_hash:
            continue
        modified.append({
            "file": name,
            "before_exists": before_exists,
            "after_exists": after_exists,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
        })
        if before_exists:
            path.write_bytes(info["bytes"])
        elif path.is_file() or path.is_symlink():
            path.unlink()
    return {
        "ok": not modified,
        "modified_files": modified,
        "restored": bool(modified),
        "message": (
            "Stage 04B must not modify frozen Stage 04A assertion-plan inputs"
            if modified
            else ""
        ),
    }


def finalize_assertion_execute_outputs(
    *,
    variables: dict[str, str],
    repo_root: Path,
    code_root: Path,
    result_dir: Path,
    logs_dir: Path,
) -> int:
    """Rebuild Stage 04B JSON artifacts from the frozen spec and raw traces.

    The isolated agent may need to inspect runtime behavior, but the committed
    assertion contract must not depend on the agent's prose or hand-written JSON.
    Use the full candidate invariant graph as the input and let
    `gt_toolkit assertions` filter it to the runtime-verified subset.
    """
    candidate_invariants = result_dir / "candidate_invariants.json"
    verified_invariants = result_dir / "verified_invariants.json"
    stdout_path = logs_dir / f"{ASSERTION_EXECUTE_STAGE}.finalize.stdout.txt"
    stderr_path = logs_dir / f"{ASSERTION_EXECUTE_STAGE}.finalize.stderr.txt"
    if candidate_invariants.exists():
        shutil.copyfile(candidate_invariants, verified_invariants)
    else:
        stderr_path.write_text(
            f"missing candidate invariant graph: {candidate_invariants}\n",
            encoding="utf-8",
        )
        stdout_path.write_text("", encoding="utf-8")
        return 2

    command = [
        sys.executable,
        "-m",
        "gt_toolkit",
        "assertions",
        "--spec",
        variables["candidate_assertions_path"],
        "--vulnerable-trace",
        str(result_dir / "vulnerable_assertion_trace.txt"),
        "--fixed-trace",
        str(result_dir / "fixed_assertion_trace.txt"),
        "--verified-invariants",
        variables["verified_invariants_path"],
        "--sanitizer-trace",
        str(result_dir / "sanitizer_trace.txt"),
        "--results-out",
        variables["assertion_results_path"],
        "--perturbation-results-out",
        variables["perturbation_results_path"],
        "--verified-assertions-out",
        variables["verified_assertions_path"],
        "--field-bindings",
        str(result_dir / "field_bindings.json"),
        "--event-locations",
        str(result_dir / "event_locations.json"),
        "--ground-truth",
        variables["gt_path"],
    ]
    env = stage_env(code_root, {"env": {}})
    env["PYTHONPATH"] = str(code_root) + os.pathsep + str(repo_root) + (
        os.pathsep + env["PYTHONPATH"]
        if env.get("PYTHONPATH") and env["PYTHONPATH"] != str(code_root)
        else ""
    )
    with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout_stream, \
            stderr_path.open("w", encoding="utf-8", errors="replace") as stderr_stream:
        proc = subprocess.run(
            command,
            cwd=repo_root,
            text=True,
            errors="replace",
            stdout=stdout_stream,
            stderr=stderr_stream,
            env=env,
        )
    if proc.returncode != 0:
        return proc.returncode

    binding_command = [
        sys.executable,
        "-m",
        "gt_toolkit",
        "assertions",
        "--check-bindings-only",
        "--spec",
        variables["candidate_assertions_path"],
        "--verified-invariants",
        variables["verified_invariants_path"],
        "--field-bindings",
        str(result_dir / "field_bindings.json"),
        "--event-locations",
        str(result_dir / "event_locations.json"),
    ]
    with stdout_path.open("a", encoding="utf-8", errors="replace") as stdout_stream, \
            stderr_path.open("a", encoding="utf-8", errors="replace") as stderr_stream:
        stdout_stream.write("\n--- bindings-only gate ---\n")
        proc = subprocess.run(
            binding_command,
            cwd=repo_root,
            text=True,
            errors="replace",
            stdout=stdout_stream,
            stderr=stderr_stream,
            env=env,
        )
    return proc.returncode


def stage_failure_kind(
    stage_name: str,
    returncode: int | None,
    required_outputs_ok: bool,
    success_check_ok: bool,
    result_dir: Path | None = None,
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
        if result_dir is not None and (result_dir / "assertion_execute_integrity.json").is_file():
            return "assertion_plan_mutated"
        if (
            result_dir is not None
            and required_outputs_ok
            and not success_check_ok
            and (
                assertion_execute_needs_execution_retry(result_dir)
                or assertion_execute_has_trace_format_error(result_dir)
            )
        ):
            return "assertion_execution_incomplete"
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
    if check.get("repo_fixed_oracle_gate"):
        path_text = str(check.get("path") or "")
        if not repo_fixed_oracle_gate_passes(variables, path_text):
            return False
    if check.get("portability_gate"):
        try:
            from gt_toolkit.portability import portability_gate_passes
        except ImportError:
            from gt_generation.gt_toolkit.portability import portability_gate_passes

        if not portability_gate_passes(variables["result_dir"]):
            return False
    if check.get("reachability_gate"):
        path_text = str(check.get("path") or "")
        if not path_text:
            return False
        path = Path(render(path_text, variables).strip("'"))
        if not path.exists():
            return False
        try:
            return reachability_gate_passes(load_json(path))
        except Exception:
            return False
    clauses = check.get("all")
    if isinstance(clauses, list):
        return bool(clauses) and all(
            _json_condition_matches(clause, variables, check.get("path"))
            for clause in clauses if isinstance(clause, dict)
        )
    return _json_condition_matches(check, variables)


def repo_fixed_oracle_gate_passes(variables: dict[str, str], report_path_text: str) -> bool:
    """Require an early fixed-side smoke oracle for repo-track samples.

    Stage 01 is primarily a reproducer gate, but repo/OSV/NVD samples with a
    declared fix commit are only useful GT candidates when the same PoC is clean
    on the fixed side. ARVO and repo samples without a fix commit are left to
    their existing stage-specific gates.
    """
    result_dir = Path(variables["result_dir"])
    prepare_path = result_dir / "prepare_report.json"
    sample_path = result_dir / "sample_info.json"
    if not prepare_path.exists() or not sample_path.exists():
        return True
    try:
        prepare = load_json(prepare_path)
        sample = load_json(sample_path)
    except Exception:
        return True
    track = str(prepare.get("track") or "")
    if not track.startswith("repo/"):
        return True
    fix_commit = str(sample.get("fix_commit") or sample.get("fixed_commit") or "").strip()
    if not fix_commit:
        return True
    if not report_path_text:
        return False
    report_path = Path(render(report_path_text, variables).strip("'"))
    if not report_path.exists():
        return False
    try:
        report = load_json(report_path)
    except Exception:
        return False

    checked = report.get("fixed_oracle_checked")
    acceptable = report.get("fixed_oracle_acceptable")
    fixed_oracle = report.get("fixed_oracle")
    if isinstance(fixed_oracle, dict):
        checked = fixed_oracle.get("checked", checked)
        acceptable = fixed_oracle.get("acceptable", acceptable)
    setup = str(report.get("setup_command") or "")
    return checked is True and acceptable is True and not setup_command_masks_failures(setup)


def setup_command_masks_failures(command: str) -> bool:
    """Reject setup commands that can hide failed dependency/build steps."""
    if re.search(r"\|\|\s*(?:true|:)(?:\s|$|[;&|)'\"`])", command):
        return True
    if re.search(r"(?:^|[\s;'\"`])set\s+\+e(?:$|[\s;'\"`])", command):
        return True
    return False


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


def reachability_gate_passes(report: dict[str, Any]) -> bool:
    """GT-generation reachability gate.

    Evaluation keeps location-reachability-v3 strict: assertion events do not
    promote a candidate PoC's R score. GT generation has stronger evidence from
    Stage 04's frozen runtime assertion trace and sanitizer oracle, so the
    workflow publish gate accepts packages where root/sink/sanitizer evidence is
    established even if an auxiliary parser/source breakpoint did not bind
    cleanly.
    """
    if report.get("reachability_checked") is not True:
        return False
    if not (
        report.get("target_vulnerability_triggered") is True
        or report.get("raw_target_vulnerability_triggered") is True
        or report.get("R5_sanitizer_triggered") is True
    ):
        return False
    raw_hits = report.get("raw_location_hits")
    if not isinstance(raw_hits, dict):
        raw_hits = {}
    hit_locations = [
        hit for hit in report.get("hit_locations", []) if isinstance(hit, dict)
    ]
    event_reachability = report.get("assertion_event_reachability")
    if not isinstance(event_reachability, dict):
        event_reachability = {}

    def hit_location_reached(*kinds: str) -> bool:
        expected = set(kinds)
        for hit in hit_locations:
            if hit.get("kind") not in expected:
                continue
            if hit.get("breakpoint_error"):
                continue
            if hit.get("hit_count") == 0:
                continue
            return True
        return False

    def assertion_role_reached(role: str) -> bool:
        for hit in hit_locations:
            if hit.get("kind") != "assertion_event":
                continue
            roles = set(hit.get("assertion_role") or [])
            if role not in roles:
                continue
            event_point = str(hit.get("event_point") or "")
            if event_reachability.get(event_point) is True:
                return True
        return False

    source_or_later_reached = any(
        item is True
        for item in (
            report.get("R2_source_reached"),
            raw_hits.get("source"),
            report.get("R3_root_cause_reached"),
            report.get("R3_root_cause_function_reached"),
            raw_hits.get("root_cause"),
        )
    ) or hit_location_reached(
        "source",
        "root_cause",
        "root_cause_function",
        "sink_function",
    ) or assertion_role_reached("root")
    if not source_or_later_reached:
        return False
    if not (
        report.get("R3_root_cause_reached") is True
        or report.get("R3_root_cause_function_reached") is True
        or raw_hits.get("root_cause") is True
        or hit_location_reached("root_cause", "root_cause_function")
        or assertion_role_reached("root")
    ):
        return False
    if report.get("R4_sink_reached") is True or report.get("R4_sink_line_reached") is True:
        return True
    if raw_hits.get("sink") is True:
        return True
    if hit_location_reached("sink", "sink_line", "sink_function"):
        return True
    return assertion_role_reached("sink")


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
    track = assertion_track(result_dir)
    required_versions: tuple[str, ...] = ()
    if stage_name == VULNERABLE_INSTRUMENTATION_STAGE:
        required_versions = ("vulnerable",)
    elif stage_name == FIXED_INSTRUMENTATION_STAGE:
        required_versions = ("fixed",)
    elif stage_name == ASSERTION_EXECUTE_STAGE:
        required_versions = ("vulnerable", "fixed")
    if track in {"arvo", "repo"} and stage_name != LEGACY_ASSERTION_STAGE:
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
            if track == "repo":
                expected_track_ok = str(build_report.get("track") or "").startswith("repo/")
            else:
                expected_track_ok = str(build_report.get("track") or "arvo") == "arvo"
            if (
                build_report.get("ok") is not True
                or not expected_track_ok
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
    if track == "arvo" and stage_name == LEGACY_ASSERTION_STAGE:
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


def assertion_track(result_dir: Path) -> str:
    try:
        prepare = load_json(result_dir / "prepare_report.json")
    except Exception:
        return ""
    track = str(prepare.get("track") or "")
    if track == "arvo":
        return "arvo"
    if track.startswith("repo/"):
        return "repo"
    return track


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
    return {
        str(s.get("name"))
        for s in prior.get("stages", [])
        if s.get("ok") and not s.get("skipped") and not s.get("dry_run")
    }


def current_resumable_ok_stages(
    *,
    stages: list[dict[str, Any]],
    prior_ok: set[str],
    config: dict[str, Any],
    sample: dict[str, Any],
    sample_path: Path,
    sample_id: str,
    repo_root: Path,
    code_root: Path,
    result_dir: Path,
) -> set[str]:
    """Only skip resumed stages whose current artifacts still pass their gate."""
    resumable: set[str] = set()
    for stage in stages:
        name = str(stage.get("name") or "")
        if not name or name not in prior_ok:
            continue
        variables = build_variables(
            stage=stage,
            config=config,
            sample=sample,
            sample_path=sample_path,
            sample_id=sample_id,
            repo_root=repo_root,
            code_root=code_root,
            result_dir=result_dir,
        )
        if current_stage_artifacts_ok(stage, variables, repo_root, code_root, result_dir):
            resumable.add(name)
    return resumable


def current_stage_artifacts_ok(
    stage: dict[str, Any],
    variables: dict[str, str],
    repo_root: Path,
    code_root: Path,
    result_dir: Path,
) -> bool:
    """Gate a resume skip using current artifacts, without freshness checks."""
    for item in stage.get("required_outputs") or []:
        path = Path(render(str(item), variables).strip("'"))
        if not path.exists():
            return False
    if str(stage.get("name") or "") == FINAL_STAGE:
        return repair_package_ready_to_publish(result_dir)
    return (
        check_success(stage, variables)
        and check_validate_gt(stage, variables, repo_root, code_root)
        and check_assertion_stage_success(stage, variables)
    )


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
