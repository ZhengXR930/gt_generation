#!/usr/bin/env python3
"""Batch skill distillation workflow for reward-framework."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluator.evaluate import evaluate_sample  # noqa: E402
from reward_framework.distillation.artifacts import load_json_if_exists, read_json, sample_project, write_json  # noqa: E402
from reward_framework.distillation.audit import audit_framework  # noqa: E402
from reward_framework.distillation.diagnosis_view import (  # noqa: E402
    DIAGNOSIS_SECTIONS,
    RUBRIC_CATEGORIES,
    RUBRIC_FINDINGS_KEY,
    learning_diagnosis,
    learning_evaluation,
)
from reward_framework.distillation.defaults import (  # noqa: E402
    DEFAULT_CODING_API_KEY_ENV,
    DEFAULT_CODING_BASE_URL,
    DEFAULT_CODING_HARNESS,
    DEFAULT_CODING_MODEL,
    DEFAULT_DSH_BASELINE_DIR,
    DEFAULT_DISTILLER_API_KEY_ENV,
    DEFAULT_DISTILLER_BASE_URL,
    DEFAULT_DISTILLER_MODEL,
    DEFAULT_ROLE_HARNESS,
    DEFAULT_RUN_ROOT,
    INITIAL_PACKET,
)
from reward_framework.distillation.outcome import classify_outcome  # noqa: E402
from reward_framework.distillation.pools import (  # noqa: E402
    build_validation_panel,
    load_pools,
    save_pools,
    teacher_view,
    update_pools,
)
from reward_framework.distillation.reference import synthesize_reference_file  # noqa: E402
from reward_framework.distillation.roles import (  # noqa: E402
    build_correction_role,
    build_curator_role,
    build_diagnosis_role,
    build_final_curator_role,
    build_final_teacher_role,
    build_teacher_role,
    execute_role,
    plan_role,
    resume_role,
)
from reward_framework.distillation.skill_packet import (  # noqa: E402
    TARGETS,
    apply_correction_decision,
    apply_curator_decisions,
    copy_initial_packet,
    lessons_snapshot,
    normalize_packet,
    read_lessons,
)
from reward_framework.distillation.splits import (  # noqa: E402
    build_chronological_split,
    freeze_split,
    load_frozen_split,
    write_split_manifest,
)

POOLS_FILE = "pools.json"


def _split(run_dir: Path) -> dict[str, Any]:
    return read_json(run_dir / "split_manifest.json")


def _batch_samples(run_dir: Path, batch_index: int) -> list[str]:
    batches = _split(run_dir).get("batches") or []
    if batch_index < 0 or batch_index >= len(batches):
        raise ValueError(f"batch index {batch_index} out of range 0..{len(batches)-1}")
    return [str(item) for item in batches[batch_index]["samples"]]


def _packet_for(run_dir: Path, batch_index: int, override: Path | None) -> Path:
    if override:
        return override
    packet = run_dir / "skill_packets" / f"batch_{batch_index:03d}"
    if packet.is_dir():
        return packet
    for previous in range(batch_index - 1, -1, -1):
        candidate = run_dir / "skill_packets" / f"batch_{previous:03d}"
        if candidate.is_dir():
            return candidate
    return INITIAL_PACKET


def _harness_command(args: argparse.Namespace, *, run_id: str, sample_file: Path, packet: Path | None) -> list[str]:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "reward_framework" / "run_harness.py"),
        "--run-id", run_id,
        "--samples-file", str(sample_file),
        "--harness", args.coding_harness,
        "--model", args.coding_model,
        "--base-url", args.base_url,
        "--api-key-env", args.api_key_env,
        "--parallel", str(args.parallel),
        "--max-iter", str(args.max_iter),
        "--max-attempts", str(args.max_attempts),
        "--timeout", str(args.timeout),
    ]
    if getattr(args, "no_skill", False):
        cmd.append("--no-skill")
    else:
        cmd += ["--skill-packet", str(packet)]
    if getattr(args, "reasoning_effort", None):
        cmd += ["--reasoning-effort", str(args.reasoning_effort)]
    if getattr(args, "overwrite", False):
        cmd.append("--overwrite")
    if getattr(args, "dry_run", False):
        cmd.append("--dry-run")
    return cmd


def _distillation_cli_command(*parts: str | Path) -> list[str]:
    return [sys.executable, "-m", "reward_framework.distillation.cli", *[str(part) for part in parts]]


def _training_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    dsh_command = str(getattr(args, "dsh_command", "") or "").strip()
    if dsh_command:
        env["GT_GENERATION_DSH_COMMAND"] = dsh_command
        try:
            first = shlex.split(dsh_command)[0]
        except ValueError:
            first = dsh_command.split()[0]
        parent = str(Path(first).expanduser().resolve().parent)
        env["PATH"] = parent + os.pathsep + env.get("PATH", "")
    return env


def _run_checked_step(
    label: str,
    cmd: list[str],
    *,
    env: dict[str, str],
    expected: Path | None = None,
    retries: int = 0,
) -> None:
    command = [str(part) for part in cmd]
    attempts = max(1, retries + 1)
    last_rc = 0
    for attempt in range(attempts):
        print(json.dumps({"step": label, "attempt": attempt + 1, "command": command}, ensure_ascii=False))
        proc = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, check=False)
        last_rc = proc.returncode
        if last_rc == 0 and (expected is None or expected.is_file()):
            return
        if attempt + 1 < attempts:
            print(json.dumps({
                "step": label,
                "status": "retrying",
                "returncode": last_rc,
                "missing_expected": str(expected) if expected and not expected.is_file() else None,
            }, ensure_ascii=False))
    raise SystemExit(json.dumps({
        "step": label,
        "status": "failed",
        "returncode": last_rc,
        "missing_expected": str(expected) if expected and not expected.is_file() else None,
    }, ensure_ascii=False))


# --------------------------------------------------------------------------- run setup


def cmd_freeze_split(args: argparse.Namespace) -> int:
    """Compute the chronological order once and pin it next to valid_gt.json."""
    from reward_framework.distillation.splits import TEST_GT, TRAIN_GT

    existing = [path for path in (TRAIN_GT, TEST_GT) if path.is_file()]
    if existing and not args.force:
        raise SystemExit(
            f"{[str(path) for path in existing]} already exist. The split is meant to be pinned "
            "once: re-freezing changes which samples are held out and makes results either side "
            "of the change incomparable. Pass --force only if that is what you intend."
        )
    split = build_chronological_split(
        args.valid_gt, commit_dates=args.commit_dates, require_dates=args.require_dates
    )
    print(json.dumps(freeze_split(split), indent=2, ensure_ascii=False))
    return 0


def _attempt_specs(args: argparse.Namespace, samples: list[str]) -> list[dict[str, Any]]:
    specs = [{"label": "first", "results_dir": args.results_dir.resolve(), "evaluation": None, "samples": samples}]
    for raw in getattr(args, "attempt", []) or []:
        if "=" not in raw:
            raise SystemExit("--attempt must be label=results_dir")
        label, path = raw.split("=", 1)
        label = label.strip()
        if not label or label == "first" or not re.match(r"^[A-Za-z0-9_.-]+$", label):
            raise SystemExit("--attempt label must be non-empty, not first, and use only A-Za-z0-9_.-")
        results_dir = Path(path).resolve()
        present = [sample_id for sample_id in samples if (results_dir / sample_id).is_dir()]
        specs.append({"label": label, "results_dir": results_dir, "evaluation": None, "samples": present})
    return specs


def _evaluate_attempt_rows(namespace: str, results_dir: Path, samples: list[str]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for sample_id in samples:
        sample_dir = results_dir / sample_id
        if not sample_dir.is_dir():
            continue
        row = evaluate_sample(namespace, sample_dir, require_analysis_quality=False)
        row["deterministic_outcome"] = classify_outcome(row)
        rows[sample_id] = row
    return rows


def _outcome_rank(outcome_record: dict[str, Any] | None) -> tuple[int, int]:
    record = outcome_record or {}
    outcome = str(record.get("outcome") or "").lower()
    if outcome == "success" or record.get("triggered") is True:
        return (3, int(record.get("submitted_unique_pocs") or 0))
    if int(record.get("submitted_unique_pocs") or 0) > 0:
        return (2, int(record.get("submitted_unique_pocs") or 0))
    if outcome in {"failure", "partial"}:
        return (1, 0)
    return (0, 0)


def _selected_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [attempt for attempt in attempts if attempt.get("diagnosis")]
    if not valid:
        return None
    return max(valid, key=lambda attempt: _outcome_rank(attempt.get("outcome_record")))


def cmd_init_run(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    split_source = "frozen_gt"
    if args.baseline_dir:
        split_payload = _copy_baseline_split(run_dir, args.baseline_dir.resolve())
        split_source = str(args.baseline_dir.resolve())
        train = [str(item) for item in split_payload.get("train") or []]
        test = [str(item) for item in split_payload.get("test") or []]
        batches = split_payload.get("batches") or []
        ordering = str(split_payload.get("ordering") or "baseline split_manifest.json")
        undated = [str(item) for item in split_payload.get("undated_samples") or []]
    else:
        split = load_frozen_split()
        write_split_manifest(run_dir / "split_manifest.json", split)
        train = split.train
        test = split.test
        batches = split.batches
        ordering = split.ordering
        undated = split.undated
    copy_initial_packet(run_dir / "skill_packets" / "batch_000", args.initial_packet)
    write_json(run_dir / POOLS_FILE, load_pools(run_dir / POOLS_FILE))
    write_json(run_dir / "run_config.json", {
        "coding_agent": {
            "harness": args.coding_harness,
            "model": args.coding_model,
            "base_url": args.coding_base_url,
            "api_key_env": args.coding_api_key_env,
        },
        "distiller": {"model": args.distiller_model, "base_url": args.base_url, "api_key_env": args.api_key_env},
        "split_source": split_source,
    })
    print(json.dumps({
        "status": "initialized",
        "run_dir": str(run_dir),
        "ordering": ordering,
        "train": len(train),
        "test": len(test),
        "batches": len(batches),
        "undated_samples": len(undated),
        "split_source": split_source,
    }, indent=2))
    return 0


def _copy_baseline_split(run_dir: Path, baseline_dir: Path) -> dict[str, Any]:
    split_file = baseline_dir / "split_manifest.json"
    if not split_file.is_file():
        raise FileNotFoundError(f"baseline split not found: {split_file}")
    split_payload = read_json(split_file)
    if not isinstance(split_payload.get("batches"), list):
        raise ValueError(f"baseline split has no batches: {split_file}")
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(split_file, run_dir / "split_manifest.json")
    summary_file = baseline_dir / "batch_summary.json"
    if summary_file.is_file():
        shutil.copy2(summary_file, run_dir / "batch_summary.json")
    return split_payload


# --------------------------------------------------------------------------- training loop


def cmd_run_batch(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    samples = _batch_samples(run_dir, args.batch_index)
    batch_dir = run_dir / f"batch_{args.batch_index:03d}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    sample_file = batch_dir / "samples.txt"
    sample_file.write_text("\n".join(samples) + "\n", encoding="utf-8")
    packet = None if getattr(args, "no_skill", False) else _packet_for(run_dir, args.batch_index, args.skill_packet)
    run_id = args.reward_run_id or f"distill_{run_dir.name}_batch_{args.batch_index:03d}"
    cmd = _harness_command(args, run_id=run_id, sample_file=sample_file, packet=packet)
    (batch_dir / "run_command.json").write_text(json.dumps(cmd, indent=2) + "\n", encoding="utf-8")
    return subprocess.run(cmd, cwd=REPO_ROOT, text=True, check=False).returncode


def cmd_evaluate_batch(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    results_dir = args.results_dir.resolve()
    rows = []
    for sample_id in _batch_samples(run_dir, args.batch_index):
        row = evaluate_sample(results_dir.parent.name, results_dir / sample_id, require_analysis_quality=False)
        row["deterministic_outcome"] = classify_outcome(row)
        rows.append(row)
    report = {"protocol": "reward-distillation-batch-eval-v1", "batch_index": args.batch_index, "rows": rows}
    write_json(run_dir / f"batch_{args.batch_index:03d}" / "evaluation.json", report)
    counts: dict[str, int] = {}
    for row in rows:
        key = row["deterministic_outcome"]["outcome"]
        counts[key] = counts.get(key, 0) + 1
    submission_success = 0
    submission_only_success = 0
    for row in rows:
        submit = ((row.get("runtime") or {}).get("submission_outcome") or {})
        deterministic = row["deterministic_outcome"]
        if submit.get("success") is True:
            submission_success += 1
            if deterministic.get("triggered") is not True:
                submission_only_success += 1
    print(json.dumps({
        "status": "evaluated",
        "rows": len(rows),
        "deterministic_outcomes": counts,
        "deterministic_triggered_samples": counts.get("success", 0),
        "submission_success_samples": submission_success,
        "submission_only_success_samples": submission_only_success,
    }, indent=2))
    return 0


def _run_role(
    args: argparse.Namespace,
    role: str,
    workspace: Path,
    build,
    out_json: Path,
    *,
    retry_note: str = "",
) -> dict[str, Any]:
    """Run one role, doing as little as the previous attempt left undone.

    A sample that already has an artifact is never re-run. A session that ended
    without writing one is re-entered in place. Only a session that failed on
    its own terms - a non-zero exit or a timeout - starts over.
    """
    if args.redo and out_json.exists():
        out_json.unlink()
    plan, reason = ("fresh", "forced by --redo") if args.redo else plan_role(workspace, out_json)
    if plan == "reuse":
        return {"status": "reused", "role": role, "reason": reason, "output": str(out_json)}

    run = resume_role(role, workspace, reason=reason) if plan == "resume" else build()
    if retry_note:
        run = type(run)(run.role, run.workspace, retry_note.strip() + "\n\n" + run.prompt, run.output_file)
    if not args.execute:
        return {"status": "workspace_prepared", "role": role, "plan": plan,
                "workspace": str(run.workspace), "output": str(out_json)}

    status = execute_role(
        run,
        harness=args.role_harness,
        model=args.role_model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        api_version=args.api_version,
        reasoning_effort=args.reasoning_effort,
        timeout=args.role_timeout,
        dsh_command=getattr(args, "dsh_command", ""),
    )
    parsed = status.pop("parsed", None)
    if parsed is not None:
        write_json(out_json, parsed)
    return {**status, "plan": plan, "output": str(out_json)}


def _sample_issue_description(sample_id: str) -> str:
    path = GT_RESULTS / sample_id / "description.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _false_positive_summary(eval_row: dict[str, Any] | None, outcome_record: dict[str, Any]) -> dict[str, Any]:
    runtime = (eval_row or {}).get("runtime") or {}
    submission = runtime.get("submission_outcome") or {}
    submission_crashed = int(submission.get("crashed_pocs") or submission.get("triggered_pocs") or 0)
    outcome_false_positives = int((outcome_record or {}).get("false_positive_pocs") or 0)
    triggered = bool((outcome_record or {}).get("triggered"))
    false_positive_pocs = 0 if triggered else max(submission_crashed, outcome_false_positives)
    return {
        "false_positive": bool(false_positive_pocs),
        "false_positive_pocs": false_positive_pocs,
    }



_FORBIDDEN_DIAGNOSIS_TERMS = (
    re.compile(r"\bR[0-5](?:_[A-Za-z0-9_]+)?\b"),
    re.compile(r"\bParser\b"),
    re.compile(r"\bSource\b"),
    re.compile(r"\bRoot Cause\b"),
    re.compile(r"\bSink\b"),
    re.compile(r"\bTrigger\b"),
    re.compile(r"\bGT\b"),
)


def _diagnosis_text_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        texts: list[str] = []
        for item in value.values():
            texts.extend(_diagnosis_text_values(item))
        return texts
    if isinstance(value, list):
        texts = []
        for item in value:
            texts.extend(_diagnosis_text_values(item))
        return texts
    if isinstance(value, str):
        return [value]
    return []


def _forbidden_diagnosis_terms(diagnosis: dict[str, Any]) -> list[str]:
    hits: set[str] = set()
    for section in DIAGNOSIS_SECTIONS:
        for text in _diagnosis_text_values(diagnosis.get(section)):
            for pattern in _FORBIDDEN_DIAGNOSIS_TERMS:
                for match in pattern.finditer(text):
                    hits.add(match.group(0))
    return sorted(hits)

def _diagnosis_retry_note(error: str) -> str:
    return (
        "Your previous OUTPUT.json was rejected by the output-quality gate: "
        f"{error}. Keep the same factual diagnosis, but rewrite the JSON values "
        "using behavior-language only. Do not use evaluator-only terms such as "
        "R0, R1, R2, R3, R4, R5, Parser, Source, Root Cause, Sink, Trigger, or GT. "
        "Use terms like accepted input, issue-relevant path, vulnerable condition, "
        "sensitive operation, observable failure, target issue, or non-target crash."
    )


def _diagnosis_quality_error(diagnosis: dict[str, Any]) -> str | None:
    if not str(diagnosis.get("issue_description") or "").strip():
        return "diagnosis missing issue_description"
    missing: list[str] = []
    for section in DIAGNOSIS_SECTIONS:
        value = diagnosis.get(section)
        if not isinstance(value, dict):
            missing.append(section)
            continue
        for field in ("summary", "evidence"):
            if not str(value.get(field) or "").strip():
                missing.append(f"{section}.{field}")
    if missing:
        return "diagnosis missing required evidence field(s): " + ", ".join(missing)
    rubric_findings = diagnosis.get(RUBRIC_FINDINGS_KEY)
    if not isinstance(rubric_findings, list):
        return f"diagnosis missing {RUBRIC_FINDINGS_KEY}"
    allowed_categories = set(RUBRIC_CATEGORIES)
    for index, finding in enumerate(rubric_findings):
        if not isinstance(finding, dict):
            return f"{RUBRIC_FINDINGS_KEY}[{index}] must be an object"
        category = str(finding.get("category") or "")
        if category not in allowed_categories:
            return f"{RUBRIC_FINDINGS_KEY}[{index}].category must be one of: {', '.join(RUBRIC_CATEGORIES)}"
        present = finding.get("present")
        if not isinstance(present, bool):
            return f"{RUBRIC_FINDINGS_KEY}[{index}].present must be boolean"
        if present and not str(finding.get("evidence") or "").strip():
            return f"{RUBRIC_FINDINGS_KEY}[{index}].evidence is required when present is true"
    forbidden = _forbidden_diagnosis_terms(diagnosis)
    if forbidden:
        return "diagnosis uses evaluator-only term(s): " + ", ".join(forbidden)
    return None


def _normalize_diagnosis_for_outcome(
    diagnosis: dict[str, Any],
    outcome_record: dict[str, Any],
    *,
    sample_id: str = "",
) -> dict[str, Any]:
    """Attach deterministic facts without accepting legacy diagnosis fields."""
    normalized: dict[str, Any] = {}
    raw_sample_id = diagnosis.get("sample_id") or sample_id
    if raw_sample_id:
        normalized["sample_id"] = raw_sample_id
    outcome = str((outcome_record or {}).get("outcome") or diagnosis.get("outcome") or "").lower()
    if outcome:
        normalized["outcome"] = outcome
    issue = str(diagnosis.get("issue_description") or "").strip()
    if not issue and sample_id:
        issue = _sample_issue_description(sample_id)
    if issue:
        normalized["issue_description"] = issue
    if diagnosis.get("vulnerability_type") not in (None, ""):
        normalized["vulnerability_type"] = diagnosis.get("vulnerability_type")
    for section in DIAGNOSIS_SECTIONS:
        if section in diagnosis:
            normalized[section] = diagnosis.get(section)
    if RUBRIC_FINDINGS_KEY in diagnosis:
        normalized[RUBRIC_FINDINGS_KEY] = diagnosis.get(RUBRIC_FINDINGS_KEY)
    return normalized

def cmd_diagnose_batch(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    batch_dir = run_dir / f"batch_{args.batch_index:03d}"
    eval_report = read_json(args.evaluation.resolve() if args.evaluation else batch_dir / "evaluation.json")
    eval_by_sample = {row["sample_id"]: row for row in eval_report.get("rows", [])}

    samples = _batch_samples(run_dir, args.batch_index)
    attempt_specs = _attempt_specs(args, samples)
    if getattr(args, "attempt_only", False):
        attempt_specs = [spec for spec in attempt_specs if spec["label"] != "first"]
        if not attempt_specs:
            raise SystemExit("--attempt-only requires at least one --attempt label=results_dir")
    for spec in attempt_specs:
        if spec["label"] == "first":
            spec["evaluation"] = eval_by_sample
        else:
            spec["evaluation"] = _evaluate_attempt_rows(
                spec["results_dir"].parent.name, spec["results_dir"], samples
            )

    def run_one(sample_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        label = str(spec["label"])
        results_dir = spec["results_dir"]
        eval_row = spec["evaluation"].get(sample_id)
        outcome_record = (eval_row or {}).get("deterministic_outcome") or classify_outcome(eval_row)
        workspace = (
            batch_dir / "subsessions" / "diagnose" / sample_id
            if label == "first"
            else batch_dir / "subsessions" / "diagnose_attempts" / label / sample_id
        )
        out_json = (
            batch_dir / "diagnoses" / f"{sample_id}.json"
            if label == "first"
            else batch_dir / "diagnoses_attempts" / label / f"{sample_id}.json"
        )
        status = _run_role(
            args, "diagnose", workspace, out_json=out_json,
            build=lambda: build_diagnosis_role(workspace, sample_id, results_dir, eval_row, outcome_record),
        )
        if args.execute and out_json.is_file():
            diagnosis = _normalize_diagnosis_for_outcome(
                read_json(out_json), outcome_record, sample_id=sample_id
            )
            quality_error = _diagnosis_quality_error(diagnosis)
            if quality_error:
                if out_json.exists():
                    out_json.unlink()
                retry_args = argparse.Namespace(**vars(args))
                retry_args.redo = True
                retry_status = _run_role(
                    retry_args, "diagnose", workspace, out_json=out_json,
                    build=lambda: build_diagnosis_role(workspace, sample_id, results_dir, eval_row, outcome_record),
                    retry_note=_diagnosis_retry_note(quality_error),
                )
                retry_status["retry_of_invalid_output"] = {
                    "status": status.get("status"),
                    "error": quality_error,
                }
                status = retry_status
        return {
            "sample_id": sample_id,
            "attempt": label,
            "status": status,
            "out_json": out_json,
            "outcome_record": outcome_record,
        }

    parallel = max(1, int(getattr(args, "parallel", 1) or 1))
    jobs = [(sample_id, spec) for spec in attempt_specs for sample_id in spec["samples"]]
    completed: dict[tuple[str, str], dict[str, Any]] = {}
    if parallel == 1 or len(jobs) <= 1:
        for sample_id, spec in jobs:
            completed[(sample_id, str(spec["label"]))] = run_one(sample_id, spec)
    else:
        with ThreadPoolExecutor(max_workers=min(parallel, len(jobs))) as executor:
            futures = {executor.submit(run_one, sample_id, spec): (sample_id, str(spec["label"])) for sample_id, spec in jobs}
            for future in as_completed(futures):
                sample_id, label = futures[future]
                try:
                    completed[(sample_id, label)] = future.result()
                except Exception as exc:  # keep the batch report materialized for partial failures
                    completed[(sample_id, label)] = {
                        "sample_id": sample_id,
                        "attempt": label,
                        "status": {"status": "error", "role": "diagnose", "error": repr(exc)},
                        "out_json": (
                            batch_dir / "diagnoses" / f"{sample_id}.json"
                            if label == "first"
                            else batch_dir / "diagnoses_attempts" / label / f"{sample_id}.json"
                        ),
                        "outcome_record": (
                            ((next(spec for spec in attempt_specs if spec["label"] == label)["evaluation"]).get(sample_id) or {}).get("deterministic_outcome")
                            or classify_outcome((next(spec for spec in attempt_specs if spec["label"] == label)["evaluation"]).get(sample_id))
                        ),
                    }

    statuses, by_sample = [], {sample_id: [] for sample_id in samples}
    for sample_id in samples:
        for spec in attempt_specs:
            label = str(spec["label"])
            if sample_id not in spec["samples"]:
                continue
            item = completed[(sample_id, label)]
            out_json = item["out_json"]
            outcome_record = item["outcome_record"]
            status_row = {"sample_id": sample_id, "attempt": label, **item["status"]}
            statuses.append(status_row)
            if not out_json.is_file():
                continue
            diagnosis = _normalize_diagnosis_for_outcome(read_json(out_json), outcome_record, sample_id=sample_id)
            quality_error = _diagnosis_quality_error(diagnosis)
            if quality_error:
                status_row["status"] = "invalid_output"
                status_row["error"] = quality_error
                write_json(out_json, {**diagnosis, "diagnosis_quality_error": quality_error})
                continue
            raw_out = (
                batch_dir / "diagnoses_raw" / f"{sample_id}.json"
                if label == "first"
                else batch_dir / "diagnoses_raw_attempts" / label / f"{sample_id}.json"
            )
            write_json(raw_out, diagnosis)
            learning = learning_diagnosis(diagnosis, outcome_record)
            write_json(out_json, learning)
            by_sample[sample_id].append({
                "attempt": label,
                "outcome_record": outcome_record,
                "diagnosis": learning,
            })

    diagnoses, pool_records = [], []
    for sample_id in samples:
        attempts = by_sample[sample_id]
        if getattr(args, "attempt_only", False):
            continue
        selected = _selected_attempt(attempts)
        if not selected:
            continue
        learning = dict(selected["diagnosis"])
        learning["selected_attempt"] = selected["attempt"]
        if len(attempts) > 1:
            learning["attempts"] = attempts
        out_json = batch_dir / "diagnoses" / f"{sample_id}.json"
        write_json(out_json, learning)
        diagnoses.append(learning)
        outcome_record = selected["outcome_record"]
        false_positive = _false_positive_summary(eval_by_sample.get(sample_id), outcome_record)
        pool_records.append({
            "sample_id": sample_id,
            "project": sample_project(sample_id),
            "outcome": learning.get("outcome"),
            **false_positive,
            "issue_description": learning.get("issue_description"),
            "vulnerability_type": learning.get("vulnerability_type"),
            **{section: learning.get(section) for section in DIAGNOSIS_SECTIONS},
            "attempts": attempts,
            "selected_attempt": selected["attempt"],
            "outcome_record": outcome_record,
        })

    if getattr(args, "attempt_only", False):
        attempt_diagnoses = []
        for sample_id in samples:
            for attempt in by_sample[sample_id]:
                learning = dict(attempt["diagnosis"])
                learning["attempt"] = attempt["attempt"]
                attempt_diagnoses.append(learning)
        write_json(batch_dir / "diagnosis_status_attempts.json", statuses)
        write_json(batch_dir / "retry_diagnoses.json", attempt_diagnoses)
        print(json.dumps({
            "status": "attempt_diagnosis",
            "items": len(statuses),
            "materialized": len(attempt_diagnoses),
        }, indent=2))
        return 0

    write_json(batch_dir / "diagnosis_status.json", statuses)
    if diagnoses:
        write_json(batch_dir / "diagnoses.json", diagnoses)
        pools_path = run_dir / POOLS_FILE
        pools = update_pools(load_pools(pools_path), args.batch_index, pool_records)
        save_pools(pools_path, pools)
    print(json.dumps({"status": "diagnosis", "items": len(statuses), "materialized": len(diagnoses)}, indent=2))
    return 0


def _diagnoses_for_window(run_dir: Path, batch_index: int, window_size: int) -> list[dict[str, Any]]:
    """Load the diagnosis window that Teacher should distill from.

    Operational batches stay at 10 samples, but distillation normally waits for
    a wider evidence window. With window_size=3, proposing at batch 2 reads
    diagnoses from batches 0, 1, and 2; the resulting packet is meant for
    batch 3.
    """
    start = _diagnosis_window_start(batch_index, window_size)
    diagnoses: list[dict[str, Any]] = []
    missing: list[str] = []
    for index in range(start, batch_index + 1):
        path = run_dir / f"batch_{index:03d}" / "diagnoses.json"
        if not path.is_file():
            missing.append(str(path))
            continue
        payload = read_json(path)
        items = payload.get("diagnoses", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise SystemExit(f"{path} must contain a diagnosis list")
        for item in items:
            if isinstance(item, dict):
                enriched = dict(item)
                enriched.setdefault("source_batch", index)
                diagnoses.append(enriched)
    if missing:
        raise SystemExit("missing diagnosis window files: " + ", ".join(missing))
    return diagnoses


def _diagnosis_window_start(batch_index: int, window_size: int) -> int:
    window_size = max(1, int(window_size or 1))
    return max(0, int(batch_index) - window_size + 1)


def _teacher_pool_cutoff(batch_index: int, window_size: int, *, explicit_diagnoses: bool = False) -> int:
    """Last pool batch visible beside the current Teacher diagnosis input.

    Teacher receives the current diagnosis payload separately. Pools should add
    earlier TRAIN experience only, not duplicate any batch already present in
    the diagnosis window.
    """
    if explicit_diagnoses:
        return int(batch_index) - 1
    return _diagnosis_window_start(batch_index, window_size) - 1


def cmd_propose_updates(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    batch_dir = run_dir / f"batch_{args.batch_index:03d}"
    packet = _packet_for(run_dir, args.batch_index, args.skill_packet)
    explicit_diagnoses = bool(args.diagnoses)
    if explicit_diagnoses:
        diagnoses = read_json(args.diagnoses.resolve())
        if isinstance(diagnoses, dict):
            diagnoses = diagnoses.get("diagnoses", diagnoses)
    else:
        diagnoses = _diagnoses_for_window(run_dir, args.batch_index, args.diagnosis_window_size)
    diagnoses = [learning_diagnosis(item) for item in diagnoses if isinstance(item, dict)]
    pools_view = teacher_view(
        load_pools(run_dir / POOLS_FILE),
        through_batch=_teacher_pool_cutoff(
            args.batch_index,
            args.diagnosis_window_size,
            explicit_diagnoses=explicit_diagnoses,
        ),
    )
    out_json = batch_dir / "teacher_candidate_updates.json"
    workspace = batch_dir / "subsessions" / "teacher"
    status = _run_role(
        args, "teacher", workspace, out_json=out_json,
        build=lambda: build_teacher_role(workspace, packet, diagnoses, pools_view),
    )
    print(json.dumps(status, indent=2))
    return 0


def cmd_curate_updates(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    batch_dir = run_dir / f"batch_{args.batch_index:03d}"
    packet = _packet_for(run_dir, args.batch_index, args.skill_packet)
    payload = read_json(args.updates.resolve() if args.updates else batch_dir / "teacher_candidate_updates.json")
    updates = payload.get("candidate_updates", payload if isinstance(payload, list) else [])
    pools_view = teacher_view(load_pools(run_dir / POOLS_FILE), through_batch=args.batch_index)
    out_json = batch_dir / "curator_decisions.json"
    workspace = batch_dir / "subsessions" / "curator"
    status = _run_role(
        args, "curator", workspace, out_json=out_json,
        build=lambda: build_curator_role(workspace, packet, updates, pools_view),
    )
    print(json.dumps(status, indent=2))
    return 0


def _next_panel_index(run_dir: Path, through_batch: int) -> int:
    root = run_dir / "validation_panels"
    prefix = f"through_batch_{through_batch:03d}_panel_"
    indexes = []
    for path in root.glob(prefix + "*"):
        try:
            indexes.append(int(path.name[len(prefix):]))
        except ValueError:
            continue
    return (max(indexes) + 1) if indexes else 0


def cmd_build_validation_panel(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    through_batch = args.through_batch
    panel_index = args.panel_index
    if panel_index is None:
        panel_index = _next_panel_index(run_dir, through_batch)
    panel = build_validation_panel(
        load_pools(run_dir / POOLS_FILE),
        through_batch=through_batch,
        success_count=args.success_count,
        crash_count=args.crash_count,
        failure_count=args.failure_count,
        panel_index=panel_index,
    )
    out_dir = args.out_dir or run_dir / "validation_panels" / f"through_batch_{through_batch:03d}_panel_{panel_index:03d}"
    samples = [str(item["sample_id"]) for item in panel.get("samples") or [] if item.get("sample_id")]
    write_json(out_dir / "panel.json", panel)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "samples.txt").write_text("\n".join(samples) + ("\n" if samples else ""), encoding="utf-8")
    print(json.dumps({
        "status": "validation_panel",
        "out_dir": str(out_dir),
        "samples": len(samples),
        "panel_index": panel_index,
        "counts": panel.get("counts"),
        "samples_file": str(out_dir / "samples.txt"),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_apply_curation(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    batch_dir = run_dir / f"batch_{args.batch_index:03d}"
    packet = _packet_for(run_dir, args.batch_index, args.skill_packet)
    payload = read_json(args.decisions.resolve() if args.decisions else batch_dir / "curator_decisions.json")
    decisions = payload.get("decisions", payload if isinstance(payload, list) else [])
    out_packet = args.out_packet or run_dir / "skill_packets" / f"batch_{args.batch_index + 1:03d}"
    result = apply_curator_decisions(packet, decisions, out_packet=out_packet)

    write_json(batch_dir / "applied_updates.json", result)
    print(json.dumps({
        "packet": result["packet"],
        "applied": result["applied"],
        "skipped": [{"target": item.get("target"), "reason": item.get("skip_reason"),
                     "lint_reasons": item.get("lint_reasons")} for item in result["skipped"]],
    }, indent=2, ensure_ascii=False))
    return 0




def _applied_lesson_ids(payload: dict[str, Any]) -> set[tuple[str, str]] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("applied"), list):
        return None
    allowed: set[tuple[str, str]] = set()
    for item in payload.get("applied") or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "")
        lesson_id = str(item.get("lesson_id") or "")
        if target and lesson_id:
            allowed.add((target, lesson_id))
    return allowed


def _read_baseline_evaluations(paths: list[str] | None) -> list[dict[str, Any]]:
    baselines: list[dict[str, Any]] = []
    for item in paths or []:
        label, sep, raw_path = item.partition("=")
        if not sep:
            raw_path = label
            label = Path(raw_path).stem
        path = Path(raw_path).expanduser().resolve()
        payload = read_json(path)
        baselines.append({"label": label, "path": str(path), "evaluation": learning_evaluation(payload)})
    return baselines


def cmd_correct_skill(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    batch_dir = run_dir / f"batch_{args.batch_index:03d}"
    previous_packet = _packet_for(run_dir, max(args.batch_index - 1, 0), args.previous_packet)
    current_packet = _packet_for(run_dir, args.batch_index, args.current_packet)
    eval_report = read_json(args.evaluation.resolve() if args.evaluation else batch_dir / "evaluation.json")
    diagnoses_path = args.diagnoses.resolve() if args.diagnoses else batch_dir / "diagnoses.json"
    diagnoses_payload = read_json(diagnoses_path) if diagnoses_path.is_file() else []
    diagnoses = diagnoses_payload.get("diagnoses", diagnoses_payload) if isinstance(diagnoses_payload, dict) else diagnoses_payload
    if not isinstance(diagnoses, list):
        raise SystemExit(f"{diagnoses_path} must contain a diagnosis list")
    applied_path = args.applied_updates.resolve() if args.applied_updates else run_dir / f"batch_{max(args.batch_index - 1, 0):03d}" / "applied_updates.json"
    diagnoses = [learning_diagnosis(item) for item in diagnoses if isinstance(item, dict)]
    eval_report = learning_evaluation(eval_report)
    applied_updates = read_json(applied_path) if applied_path.is_file() else {}
    pools_view = teacher_view(load_pools(run_dir / POOLS_FILE), through_batch=args.batch_index - 1)
    out_json = batch_dir / "correction_decision.json"
    workspace = batch_dir / "subsessions" / "correction"
    status = _run_role(
        args, "correction", workspace, out_json=out_json,
        build=lambda: build_correction_role(
            workspace,
            previous_packet,
            current_packet,
            eval_report,
            diagnoses,
            applied_updates,
            pools_view,
            _read_baseline_evaluations(args.baseline_evaluation),
        ),
    )
    print(json.dumps(status, indent=2))
    return 0


def cmd_apply_correction(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    batch_dir = run_dir / f"batch_{args.batch_index:03d}"
    previous_packet = _packet_for(run_dir, max(args.batch_index - 1, 0), args.previous_packet)
    current_packet = _packet_for(run_dir, args.batch_index, args.current_packet)
    correction = read_json(args.correction.resolve() if args.correction else batch_dir / "correction_decision.json")
    out_packet = args.out_packet or run_dir / "skill_packets" / f"batch_{args.batch_index + 1:03d}"
    result = apply_correction_decision(
        current_packet,
        previous_packet,
        correction,
        out_packet=out_packet,
    )
    write_json(batch_dir / "correction_applied.json", result)
    print(json.dumps({
        "packet": result["packet"],
        "decision": result["decision"],
        "applied": result["applied"],
        "skipped": result["skipped"],
    }, indent=2, ensure_ascii=False))
    return 0


def _write_final_skill_packet(source_packet: Path, decision: dict[str, Any], out_packet: Path) -> dict[str, Any]:
    def normalize_final_markdown(text: str) -> str:
        text = text.strip()
        # Some role outputs preserve stable ids but forget the markdown bullet;
        # packet parsing requires lessons to be written as "- [R.B#n] ...".
        return re.sub(r"(?m)^(\s*)\[((?:R\.B|S\.C)#\d+)\]\s+", r"\1- [\2] ", text)

    repro = normalize_final_markdown(str(decision.get("final_reproduction_skill_md") or ""))
    submit = normalize_final_markdown(str(decision.get("final_submission_skill_md") or ""))
    if not repro or not submit:
        return {"written": False, "reason": "final curator output did not include both final SKILL.md contents"}
    if out_packet.exists():
        shutil.rmtree(out_packet)
    shutil.copytree(source_packet, out_packet, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (out_packet / "reproduction_skill").mkdir(parents=True, exist_ok=True)
    (out_packet / "submission_skill").mkdir(parents=True, exist_ok=True)
    (out_packet / "reproduction_skill" / "SKILL.md").write_text(repro + chr(10), encoding="utf-8")
    (out_packet / "submission_skill" / "SKILL.md").write_text(submit + chr(10), encoding="utf-8")
    write_json(out_packet / "FINAL_CURATION.json", {
        "decision": decision.get("decision"),
        "rationale": decision.get("rationale"),
        "lesson_decisions": decision.get("lesson_decisions") or [],
    })
    normalize_packet(out_packet)
    return {"written": True, "packet": str(out_packet), "lessons_after": lessons_snapshot(out_packet)}


def cmd_finalize_skill(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    through_batch = args.through_batch
    latest_packet = args.skill_packet.resolve() if args.skill_packet else run_dir / "skill_packets" / f"batch_{through_batch + 1:03d}"
    if not latest_packet.is_dir():
        latest_packet = _packet_for(run_dir, through_batch, args.skill_packet)
    initial_packet = args.initial_packet.resolve() if args.initial_packet else INITIAL_PACKET
    final_dir = (args.out_dir or run_dir / "finalization" / f"through_batch_{through_batch:03d}").resolve()
    comparison_paths = [Path(item).expanduser().resolve() for item in args.comparison]

    teacher_out = final_dir / "final_teacher_review.json"
    teacher_workspace = final_dir / "subsessions" / "final_teacher"
    teacher_status = _run_role(
        args,
        "final_teacher",
        teacher_workspace,
        out_json=teacher_out,
        build=lambda: build_final_teacher_role(
            teacher_workspace,
            initial_packet=initial_packet,
            latest_packet=latest_packet,
            run_dir=run_dir,
            through_batch=through_batch,
            comparison_paths=comparison_paths,
        ),
    )

    result: dict[str, Any] = {"final_teacher": teacher_status}
    if teacher_status.get("status") == "workspace_prepared":
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if teacher_status.get("status") not in {"ok", "reused"}:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1

    curator_out = final_dir / "final_curator_decision.json"
    curator_workspace = final_dir / "subsessions" / "final_curator"
    curator_status = _run_role(
        args,
        "final_curator",
        curator_workspace,
        out_json=curator_out,
        build=lambda: build_final_curator_role(
            curator_workspace,
            initial_packet=initial_packet,
            latest_packet=latest_packet,
            run_dir=run_dir,
            through_batch=through_batch,
            final_teacher_review=teacher_out,
            comparison_paths=comparison_paths,
        ),
    )
    result["final_curator"] = curator_status

    curator_decision = read_json(curator_out) if curator_status.get("status") in {"ok", "reused"} and curator_out.is_file() else None
    if isinstance(curator_decision, dict):
        out_packet = args.final_packet.resolve() if args.final_packet else final_dir / "skill_packet"
        result["final_packet"] = _write_final_skill_packet(
            latest_packet,
            curator_decision,
            out_packet,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if curator_status.get("status") in {"ok", "reused"} else 1


# --------------------------------------------------------------------------- automatic training


def _append_role_cli_args(cmd: list[str], args: argparse.Namespace) -> list[str]:
    cmd.extend([
        "--execute",
        "--role-harness", args.role_harness,
        "--role-model", args.role_model,
        "--base-url", args.role_base_url,
        "--api-key-env", args.role_api_key_env,
        "--reasoning-effort", args.role_reasoning_effort,
        "--role-timeout", str(args.role_timeout),
    ])
    if args.role_api_version:
        cmd.extend(["--api-version", args.role_api_version])
    if getattr(args, "dsh_command", ""):
        cmd.extend(["--dsh-command", args.dsh_command])
    if args.redo_roles:
        cmd.append("--redo")
    return cmd


def _append_harness_cli_args(cmd: list[str], args: argparse.Namespace) -> list[str]:
    cmd.extend([
        "--coding-harness", args.coding_harness,
        "--coding-model", args.coding_model,
        "--base-url", args.coding_base_url,
        "--api-key-env", args.coding_api_key_env,
        "--parallel", str(args.parallel),
        "--max-iter", str(args.max_iter),
        "--max-attempts", str(args.max_attempts),
        "--timeout", str(args.timeout),
        "--reasoning-effort", args.reasoning_effort,
    ])
    if args.overwrite:
        cmd.append("--overwrite")
    return cmd


def _reward_run_id(args: argparse.Namespace, run_dir: Path, batch_index: int) -> str:
    if args.reward_run_prefix:
        return f"{args.reward_run_prefix}_batch_{batch_index:03d}"
    return f"distill_{run_dir.name}_batch_{batch_index:03d}"


def _batch_results_dir(run_id: str) -> Path:
    return REPO_ROOT / "reward_framework" / "harness_runs" / run_id / "results"


def _archive_pre_correction(batch_dir: Path) -> None:
    for name in (
        "evaluation.json",
        "diagnoses.json",
        "diagnosis_status.json",
    ):
        path = batch_dir / name
        if path.is_file():
            shutil.copy2(path, batch_dir / f"pre_correction_{name}")


def _archive_post_correction(batch_dir: Path) -> None:
    for name in (
        "evaluation.json",
        "diagnoses.json",
        "diagnosis_status.json",
    ):
        path = batch_dir / name
        if path.is_file():
            shutil.copy2(path, batch_dir / f"post_correction_{name}")


def _restore_pre_correction(batch_dir: Path) -> list[str]:
    restored: list[str] = []
    for name in (
        "evaluation.json",
        "diagnoses.json",
        "diagnosis_status.json",
    ):
        source = batch_dir / f"pre_correction_{name}"
        if source.is_file():
            shutil.copy2(source, batch_dir / name)
            restored.append(name)
    return restored


def _deterministic_gt_success_count(evaluation: Path | dict[str, Any]) -> int:
    payload = read_json(evaluation) if isinstance(evaluation, Path) else evaluation
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        total = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            outcome = row.get("deterministic_outcome") or {}
            if isinstance(outcome, dict) and (outcome.get("outcome") == "success" or outcome.get("triggered") is True):
                total += 1
        return total
    counts = payload.get("deterministic_outcomes") if isinstance(payload, dict) else None
    if not isinstance(counts, dict):
        counts = payload.get("outcomes") if isinstance(payload, dict) else None
    return int((counts or {}).get("success") or 0)


def _evaluated_rows_count(evaluation: Path | dict[str, Any]) -> int:
    payload = read_json(evaluation) if isinstance(evaluation, Path) else evaluation
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        return len(rows)
    if isinstance(payload, dict):
        return int(payload.get("rows") or payload.get("evaluated") or 0)
    return 0


def _gate_correction_result(
    *,
    batch_dir: Path,
    pre_evaluation: Path,
    post_evaluation: Path,
    pre_results_dir: Path,
    post_results_dir: Path,
    pre_packet: Path,
    post_packet: Path,
) -> dict[str, Any]:
    pre_success = _deterministic_gt_success_count(pre_evaluation)
    post_success = _deterministic_gt_success_count(post_evaluation)
    decision = "ACCEPT" if post_success >= pre_success else "REVERT"
    restored: list[str] = []
    if decision == "REVERT":
        restored = _restore_pre_correction(batch_dir)
    gate = {
        "protocol": "correction-gate-v1",
        "metric": "deterministic_gt_success",
        "decision": decision,
        "pre_correction": {
            "gt_success": pre_success,
            "evaluated_rows": _evaluated_rows_count(pre_evaluation),
            "evaluation": str(pre_evaluation),
            "results_dir": str(pre_results_dir),
            "skill_packet": str(pre_packet),
        },
        "post_correction": {
            "gt_success": post_success,
            "evaluated_rows": _evaluated_rows_count(post_evaluation),
            "evaluation": str(post_evaluation),
            "results_dir": str(post_results_dir),
            "skill_packet": str(post_packet),
        },
        "restored_artifacts": restored,
    }
    write_json(batch_dir / "correction_gate.json", gate)
    return gate


def _run_batch_generation(
    args: argparse.Namespace,
    *,
    run_dir: Path,
    batch_index: int,
    packet: Path,
    run_id: str,
    env: dict[str, str],
) -> Path:
    cmd = _distillation_cli_command(
        "run-batch",
        "--run-dir", run_dir,
        "--batch-index", batch_index,
        "--skill-packet", packet,
        "--reward-run-id", run_id,
    )
    _append_harness_cli_args(cmd, args)
    _run_checked_step(f"batch_{batch_index:03d}:run", cmd, env=env)
    return _batch_results_dir(run_id)


def _run_batch_evaluation(
    *,
    run_dir: Path,
    batch_index: int,
    results_dir: Path,
    env: dict[str, str],
) -> Path:
    out = run_dir / f"batch_{batch_index:03d}" / "evaluation.json"
    cmd = _distillation_cli_command(
        "evaluate-batch",
        "--run-dir", run_dir,
        "--batch-index", batch_index,
        "--results-dir", results_dir,
    )
    _run_checked_step(f"batch_{batch_index:03d}:evaluate", cmd, env=env, expected=out)
    return out


def _run_batch_diagnosis(
    args: argparse.Namespace,
    *,
    run_dir: Path,
    batch_index: int,
    results_dir: Path,
    evaluation: Path,
    env: dict[str, str],
    redo: bool = False,
) -> Path:
    out = run_dir / f"batch_{batch_index:03d}" / "diagnoses.json"
    cmd = _distillation_cli_command(
        "diagnose-batch",
        "--run-dir", run_dir,
        "--batch-index", batch_index,
        "--results-dir", results_dir,
        "--evaluation", evaluation,
        "--parallel", str(args.role_parallel),
    )
    _append_role_cli_args(cmd, args)
    if redo and "--redo" not in cmd:
        cmd.append("--redo")
    _run_checked_step(
        f"batch_{batch_index:03d}:diagnose",
        cmd,
        env=env,
        expected=out,
        retries=args.role_retries,
    )
    return out


def _run_batch_correction(
    args: argparse.Namespace,
    *,
    run_dir: Path,
    batch_index: int,
    previous_packet: Path,
    current_packet: Path,
    evaluation: Path,
    diagnoses: Path,
    env: dict[str, str],
) -> Path:
    batch_dir = run_dir / f"batch_{batch_index:03d}"
    out = batch_dir / "correction_decision.json"
    cmd = _distillation_cli_command(
        "correct-skill",
        "--run-dir", run_dir,
        "--batch-index", batch_index,
        "--previous-packet", previous_packet,
        "--current-packet", current_packet,
        "--evaluation", evaluation,
        "--diagnoses", diagnoses,
    )
    applied = run_dir / f"batch_{max(batch_index - 1, 0):03d}" / "applied_updates.json"
    if applied.is_file():
        cmd.extend(["--applied-updates", applied])
    baseline = batch_dir / "baseline_deepseek_harness_v4_flash_evaluation.json"
    if baseline.is_file():
        cmd.extend(["--baseline-evaluation", f"historical={baseline}"])
    _append_role_cli_args(cmd, args)
    _run_checked_step(
        f"batch_{batch_index:03d}:correction",
        cmd,
        env=env,
        expected=out,
        retries=args.role_retries,
    )
    return out


def _apply_batch_correction(
    *,
    run_dir: Path,
    batch_index: int,
    previous_packet: Path,
    current_packet: Path,
    correction: Path,
    out_packet: Path,
    env: dict[str, str],
) -> Path:
    cmd = _distillation_cli_command(
        "apply-correction",
        "--run-dir", run_dir,
        "--batch-index", batch_index,
        "--previous-packet", previous_packet,
        "--current-packet", current_packet,
        "--correction", correction,
        "--out-packet", out_packet,
    )
    applied = run_dir / f"batch_{max(batch_index - 1, 0):03d}" / "applied_updates.json"
    if applied.is_file():
        cmd.extend(["--applied-updates", applied])
    _run_checked_step(
        f"batch_{batch_index:03d}:apply-correction",
        cmd,
        env=env,
        expected=out_packet / "reproduction_skill" / "SKILL.md",
    )
    return out_packet


def _run_teacher_curator_apply(
    args: argparse.Namespace,
    *,
    run_dir: Path,
    batch_index: int,
    packet: Path,
    env: dict[str, str],
) -> Path:
    batch_dir = run_dir / f"batch_{batch_index:03d}"
    updates = batch_dir / "teacher_candidate_updates.json"
    propose = _distillation_cli_command(
        "propose-updates",
        "--run-dir", run_dir,
        "--batch-index", batch_index,
        "--skill-packet", packet,
        "--diagnosis-window-size", args.diagnosis_window_size,
    )
    _append_role_cli_args(propose, args)
    _run_checked_step(
        f"batch_{batch_index:03d}:teacher",
        propose,
        env=env,
        expected=updates,
        retries=args.role_retries,
    )

    decisions = batch_dir / "curator_decisions.json"
    curate = _distillation_cli_command(
        "curate-updates",
        "--run-dir", run_dir,
        "--batch-index", batch_index,
        "--skill-packet", packet,
        "--updates", updates,
    )
    _append_role_cli_args(curate, args)
    _run_checked_step(
        f"batch_{batch_index:03d}:curator",
        curate,
        env=env,
        expected=decisions,
        retries=args.role_retries,
    )

    out_packet = run_dir / "skill_packets" / f"batch_{batch_index + 1:03d}"
    apply_cmd = _distillation_cli_command(
        "apply-curation",
        "--run-dir", run_dir,
        "--batch-index", batch_index,
        "--skill-packet", packet,
        "--decisions", decisions,
        "--out-packet", out_packet,
    )
    _run_checked_step(
        f"batch_{batch_index:03d}:apply-curation",
        apply_cmd,
        env=env,
        expected=out_packet / "reproduction_skill" / "SKILL.md",
    )
    return out_packet


def cmd_train_batches(args: argparse.Namespace) -> int:
    """Run the batch-level correction and distillation loop end to end."""
    run_dir = args.run_dir.resolve()
    if args.end_batch < args.start_batch:
        raise SystemExit("--end-batch must be >= --start-batch")
    env = _training_env(args)
    loop_events: list[dict[str, Any]] = []
    for batch_index in range(args.start_batch, args.end_batch + 1):
        batch_dir = run_dir / f"batch_{batch_index:03d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        current_packet = _packet_for(run_dir, batch_index, None)
        previous_packet = _packet_for(run_dir, max(batch_index - 1, 0), None)
        run_id = _reward_run_id(args, run_dir, batch_index)

        results_dir = _run_batch_generation(
            args,
            run_dir=run_dir,
            batch_index=batch_index,
            packet=current_packet,
            run_id=run_id,
            env=env,
        )
        evaluation = _run_batch_evaluation(
            run_dir=run_dir,
            batch_index=batch_index,
            results_dir=results_dir,
            env=env,
        )
        diagnoses = _run_batch_diagnosis(
            args,
            run_dir=run_dir,
            batch_index=batch_index,
            results_dir=results_dir,
            evaluation=evaluation,
            env=env,
        )

        correction_action = "SKIPPED"
        active_packet = current_packet
        corrected_run_id = ""
        correction_gate: dict[str, Any] | None = None
        if batch_index > 0 and not args.skip_correction:
            correction = _run_batch_correction(
                args,
                run_dir=run_dir,
                batch_index=batch_index,
                previous_packet=previous_packet,
                current_packet=current_packet,
                evaluation=evaluation,
                diagnoses=diagnoses,
                env=env,
            )
            correction_payload = read_json(correction)
            correction_action = str(correction_payload.get("decision") or "").upper()
            if correction_action in {"CORRECT", "ROLLBACK"}:
                _archive_pre_correction(batch_dir)
                pre_results_dir = results_dir
                pre_evaluation = batch_dir / "pre_correction_evaluation.json"
                pre_diagnoses = batch_dir / "pre_correction_diagnoses.json"
                corrected_packet = run_dir / "skill_packets" / f"batch_{batch_index:03d}_after_correction"
                active_packet = _apply_batch_correction(
                    run_dir=run_dir,
                    batch_index=batch_index,
                    previous_packet=previous_packet,
                    current_packet=current_packet,
                    correction=correction,
                    out_packet=corrected_packet,
                    env=env,
                )
                corrected_run_id = f"{run_id}_after_correction"
                results_dir = _run_batch_generation(
                    args,
                    run_dir=run_dir,
                    batch_index=batch_index,
                    packet=active_packet,
                    run_id=corrected_run_id,
                    env=env,
                )
                evaluation = _run_batch_evaluation(
                    run_dir=run_dir,
                    batch_index=batch_index,
                    results_dir=results_dir,
                    env=env,
                )
                diagnoses = _run_batch_diagnosis(
                    args,
                    run_dir=run_dir,
                    batch_index=batch_index,
                    results_dir=results_dir,
                    evaluation=evaluation,
                    env=env,
                    redo=True,
                )
                _archive_post_correction(batch_dir)
                correction_gate = _gate_correction_result(
                    batch_dir=batch_dir,
                    pre_evaluation=pre_evaluation,
                    post_evaluation=batch_dir / "post_correction_evaluation.json",
                    pre_results_dir=pre_results_dir,
                    post_results_dir=results_dir,
                    pre_packet=current_packet,
                    post_packet=active_packet,
                )
                if correction_gate["decision"] == "REVERT":
                    active_packet = current_packet
                    results_dir = pre_results_dir
                    evaluation = batch_dir / "evaluation.json"
                    diagnoses = batch_dir / "diagnoses.json"
                    correction_action = f"{correction_action}_REVERTED_BY_GATE"
                else:
                    evaluation = batch_dir / "evaluation.json"
                    diagnoses = batch_dir / "diagnoses.json"
            elif correction_action != "KEEP":
                raise SystemExit(f"unsupported correction decision {correction_action!r}")

        next_packet = _run_teacher_curator_apply(
            args,
            run_dir=run_dir,
            batch_index=batch_index,
            packet=active_packet,
            env=env,
        )
        event = {
            "batch_index": batch_index,
            "run_id": run_id,
            "corrected_run_id": corrected_run_id or None,
            "results_dir": str(results_dir),
            "evaluation": str(evaluation),
            "diagnoses": str(diagnoses),
            "packet_used_for_final_batch": str(active_packet),
            "correction_action": correction_action,
            "correction_gate": correction_gate,
            "next_packet": str(next_packet),
        }
        loop_events.append(event)
        write_json(batch_dir / "training_loop.json", event)
        write_json(run_dir / "training_loop_status.json", {"events": loop_events})
        print(json.dumps({"status": "batch_complete", **event}, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- held-out test


def _bare_packet(source: Path, out: Path) -> Path:
    """Control arm: the same scaffold with every learned lesson removed."""
    copy_initial_packet(out, source)
    for target, (rel, _heading, _cap) in TARGETS.items():
        path = out / rel
        if not path.is_file():
            continue
        lessons = read_lessons(out).get(target, [])
        if not lessons:
            continue
        keep = []
        drop = {"- [{}] ".format(item["lesson_id"]) for item in lessons}
        for line in path.read_text(encoding="utf-8").splitlines():
            if any(line.startswith(prefix) for prefix in drop):
                continue
            keep.append(line)
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return out


def cmd_run_test(args: argparse.Namespace) -> int:
    """Run the held-out test split once, with one frozen packet. No learning."""
    run_dir = args.run_dir.resolve()
    samples = [str(item) for item in _split(run_dir).get("test") or []]
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        raise ValueError("split_manifest.json has no test samples")

    arm_dir = run_dir / "test" / args.arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    sample_file = arm_dir / "samples.txt"
    sample_file.write_text("\n".join(samples) + "\n", encoding="utf-8")

    if args.bare:
        packet = _bare_packet(args.packet or INITIAL_PACKET, arm_dir / "packet")
    elif args.packet:
        packet = args.packet.resolve()
    else:
        packets = sorted((run_dir / "skill_packets").glob("batch_*"))
        if not packets:
            raise ValueError("no skill packet found; pass --packet")
        packet = packets[-1]
    normalize_packet(packet)

    run_id = args.reward_run_id or f"test_{run_dir.name}_{args.arm}"
    cmd = _harness_command(args, run_id=run_id, sample_file=sample_file, packet=packet)
    write_json(arm_dir / "run_command.json", {
        "arm": args.arm,
        "packet": str(packet),
        "lessons": lessons_snapshot(packet),
        "samples": len(samples),
        "command": cmd,
    })
    print(json.dumps({"arm": args.arm, "packet": str(packet), "samples": len(samples)}, indent=2))
    return subprocess.run(cmd, cwd=REPO_ROOT, text=True, check=False).returncode


def cmd_evaluate_test(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    results_dir = args.results_dir.resolve()
    samples = [str(item) for item in _split(run_dir).get("test") or []]
    rows, counts = [], {}
    for sample_id in samples:
        if not (results_dir / sample_id).is_dir():
            continue
        row = evaluate_sample(results_dir.parent.name, results_dir / sample_id, require_analysis_quality=False)
        row["deterministic_outcome"] = classify_outcome(row)
        rows.append(row)
        key = row["deterministic_outcome"]["outcome"]
        counts[key] = counts.get(key, 0) + 1
    report = {
        "protocol": "reward-distillation-test-eval-v1",
        "arm": args.arm,
        "evaluated": len(rows),
        "outcomes": counts,
        "trigger_rate": (counts.get("success", 0) / len(rows)) if rows else None,
        "rows": rows,
    }
    write_json(run_dir / "test" / args.arm / "evaluation.json", report)
    print(json.dumps({k: report[k] for k in ("arm", "evaluated", "outcomes", "trigger_rate")}, indent=2))
    return 0


def cmd_synthesize_reference(args: argparse.Namespace) -> int:
    out = args.out_file
    if out is None:
        out_dir = args.out_dir or args.run_dir / "reference_trajectories"
        out = out_dir / args.sample_id / "reference_trajectory.json"
    reference = synthesize_reference_file(args.sample_id, out.resolve())
    print(json.dumps({
        "status": "reference_synthesized",
        "sample_id": args.sample_id,
        "out_file": str(out.resolve()),
        "reference_steps": len(reference.get("reference_trace") or []),
        "context_targets": len(reference.get("context_targets") or []),
        "constraints": len(reference.get("candidate_constraints") or []),
    }, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- audit


def cmd_audit_plan(args: argparse.Namespace) -> int:
    del args
    report = audit_framework()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["errors"] else 0


# --------------------------------------------------------------------------- argv


def _add_role_args(parser: argparse.ArgumentParser) -> None:
    """Role subsessions can run through DSH or Codex."""
    parser.add_argument("--execute", action="store_true",
                        help="run the role subsession; without it only the role workspace is prepared")
    parser.add_argument("--redo", action="store_true",
                        help="start every role over, discarding artifacts an earlier run produced")
    parser.add_argument("--role-harness", default=DEFAULT_ROLE_HARNESS,
                        choices=("deepseek_harness", "dsh", "codex"))
    parser.add_argument("--role-model", default=DEFAULT_DISTILLER_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_DISTILLER_BASE_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_DISTILLER_API_KEY_ENV)
    parser.add_argument("--api-version", default="2024-03-01-preview")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--role-timeout", type=int, default=3600)
    parser.add_argument("--dsh-command", default="",
                        help="optional DeepSeek Harness CLI command for role subsessions")


def _add_harness_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--coding-harness", default=DEFAULT_CODING_HARNESS)
    parser.add_argument("--coding-model", default=DEFAULT_CODING_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_CODING_BASE_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_CODING_API_KEY_ENV)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=10800)
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reward-run-id")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-run")
    init.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_ROOT / "default")
    init.add_argument("--initial-packet", type=Path, default=INITIAL_PACKET)
    init.add_argument(
        "--baseline-dir",
        type=Path,
        default=None,
        help=(
            "optional tracked baseline directory containing split_manifest.json; "
            f"for current DSH experiments use {DEFAULT_DSH_BASELINE_DIR}"
        ),
    )
    init.add_argument("--coding-harness", default=DEFAULT_CODING_HARNESS)
    init.add_argument("--coding-model", default=DEFAULT_CODING_MODEL)
    init.add_argument("--coding-base-url", default=DEFAULT_CODING_BASE_URL)
    init.add_argument("--coding-api-key-env", default=DEFAULT_CODING_API_KEY_ENV)
    init.add_argument("--distiller-model", default=DEFAULT_DISTILLER_MODEL)
    init.add_argument("--base-url", default=DEFAULT_DISTILLER_BASE_URL)
    init.add_argument("--api-key-env", default=DEFAULT_DISTILLER_API_KEY_ENV)
    init.set_defaults(func=cmd_init_run)

    run_batch = sub.add_parser("run-batch")
    run_batch.add_argument("--run-dir", type=Path, required=True)
    run_batch.add_argument("--batch-index", type=int, required=True)
    run_batch.add_argument("--skill-packet", type=Path)
    run_batch.add_argument("--no-skill", action="store_true")
    _add_harness_args(run_batch)
    run_batch.set_defaults(func=cmd_run_batch)

    evaluate = sub.add_parser("evaluate-batch")
    evaluate.add_argument("--run-dir", type=Path, required=True)
    evaluate.add_argument("--batch-index", type=int, required=True)
    evaluate.add_argument("--results-dir", type=Path, required=True)
    evaluate.set_defaults(func=cmd_evaluate_batch)

    diagnose = sub.add_parser("diagnose-batch")
    diagnose.add_argument("--run-dir", type=Path, required=True)
    diagnose.add_argument("--batch-index", type=int, required=True)
    diagnose.add_argument("--results-dir", type=Path, required=True)
    diagnose.add_argument("--evaluation", type=Path)
    diagnose.add_argument("--attempt", action="append", default=[],
                          help="optional retry attempt as label=results_dir; may be repeated")
    diagnose.add_argument("--attempt-only", action="store_true",
                          help="diagnose only the supplied retry attempts and leave batch diagnoses/pools untouched")
    diagnose.add_argument("--parallel", type=int, default=1)
    _add_role_args(diagnose)
    diagnose.set_defaults(func=cmd_diagnose_batch)

    propose = sub.add_parser("propose-updates")
    propose.add_argument("--run-dir", type=Path, required=True)
    propose.add_argument("--batch-index", type=int, required=True)
    propose.add_argument("--skill-packet", type=Path)
    propose.add_argument("--diagnoses", type=Path)
    propose.add_argument("--diagnosis-window-size", type=int, default=3,
                         help="number of recent operational batches to feed Teacher when --diagnoses is omitted")
    _add_role_args(propose)
    propose.set_defaults(func=cmd_propose_updates)

    curate = sub.add_parser("curate-updates")
    curate.add_argument("--run-dir", type=Path, required=True)
    curate.add_argument("--batch-index", type=int, required=True)
    curate.add_argument("--skill-packet", type=Path)
    curate.add_argument("--updates", type=Path)
    _add_role_args(curate)
    curate.set_defaults(func=cmd_curate_updates)

    apply_cmd = sub.add_parser("apply-curation")
    apply_cmd.add_argument("--run-dir", type=Path, required=True)
    apply_cmd.add_argument("--batch-index", type=int, required=True)
    apply_cmd.add_argument("--skill-packet", type=Path)
    apply_cmd.add_argument("--decisions", type=Path)
    apply_cmd.add_argument("--out-packet", type=Path)
    apply_cmd.set_defaults(func=cmd_apply_curation)

    correct = sub.add_parser("correct-skill")
    correct.add_argument("--run-dir", type=Path, required=True)
    correct.add_argument("--batch-index", type=int, required=True)
    correct.add_argument("--previous-packet", type=Path)
    correct.add_argument("--current-packet", type=Path)
    correct.add_argument("--evaluation", type=Path)
    correct.add_argument("--diagnoses", type=Path)
    correct.add_argument("--applied-updates", type=Path)
    correct.add_argument("--baseline-evaluation", action="append", default=[],
                         help="optional label=path comparison evaluation; may be repeated")
    _add_role_args(correct)
    correct.set_defaults(func=cmd_correct_skill)

    apply_correction = sub.add_parser("apply-correction")
    apply_correction.add_argument("--run-dir", type=Path, required=True)
    apply_correction.add_argument("--batch-index", type=int, required=True)
    apply_correction.add_argument("--previous-packet", type=Path)
    apply_correction.add_argument("--current-packet", type=Path)
    apply_correction.add_argument("--correction", type=Path)
    apply_correction.add_argument("--applied-updates", type=Path)
    apply_correction.add_argument("--out-packet", type=Path)
    apply_correction.set_defaults(func=cmd_apply_correction)

    finalize = sub.add_parser("finalize-skill")
    finalize.add_argument("--run-dir", type=Path, required=True)
    finalize.add_argument("--through-batch", type=int, required=True,
                          help="last TRAIN batch whose update produced the latest packet")
    finalize.add_argument("--skill-packet", type=Path,
                          help="latest trained packet; defaults to skill_packets/batch_{through_batch+1}")
    finalize.add_argument("--initial-packet", type=Path,
                          help="initial packet used as context only")
    finalize.add_argument("--comparison", action="append", default=[],
                          help="optional held-out/validation comparison JSON; may be repeated")
    finalize.add_argument("--out-dir", type=Path)
    finalize.add_argument("--final-packet", type=Path,
                          help="where to materialize the final deployable skill packet; defaults to out-dir/skill_packet")
    _add_role_args(finalize)
    finalize.set_defaults(func=cmd_finalize_skill)

    train = sub.add_parser(
        "train-batches",
        help="run batch generation, diagnosis, correction, teacher, curator, and skill application",
    )
    train.add_argument("--run-dir", type=Path, required=True)
    train.add_argument("--start-batch", type=int, required=True)
    train.add_argument("--end-batch", type=int, required=True,
                       help="inclusive last batch index")
    train.add_argument("--skip-correction", action="store_true",
                       help="run teacher/curator directly without first-round correction checks")
    train.add_argument("--diagnosis-window-size", type=int, default=3)
    train.add_argument("--coding-harness", default=DEFAULT_CODING_HARNESS)
    train.add_argument("--coding-model", default=DEFAULT_CODING_MODEL)
    train.add_argument("--coding-base-url", default=DEFAULT_CODING_BASE_URL)
    train.add_argument("--coding-api-key-env", default=DEFAULT_CODING_API_KEY_ENV)
    train.add_argument("--parallel", type=int, default=3)
    train.add_argument("--max-iter", type=int, default=100)
    train.add_argument("--max-attempts", type=int, default=1)
    train.add_argument("--timeout", type=int, default=10800)
    train.add_argument("--reasoning-effort", default="medium")
    train.add_argument("--overwrite", action="store_true")
    train.add_argument("--reward-run-prefix", default="")
    train.add_argument("--dsh-command", default="",
                       help="optional DeepSeek Harness CLI command exported as GT_GENERATION_DSH_COMMAND")
    train.add_argument("--role-harness", default=DEFAULT_ROLE_HARNESS,
                       choices=("deepseek_harness", "dsh", "codex"))
    train.add_argument("--role-model", default=DEFAULT_DISTILLER_MODEL)
    train.add_argument("--role-base-url", default=DEFAULT_DISTILLER_BASE_URL)
    train.add_argument("--role-api-key-env", default=DEFAULT_DISTILLER_API_KEY_ENV)
    train.add_argument("--role-api-version", default="")
    train.add_argument("--role-reasoning-effort", default="medium")
    train.add_argument("--role-timeout", type=int, default=1800)
    train.add_argument("--role-parallel", type=int, default=3)
    train.add_argument("--role-retries", type=int, default=1,
                       help="retry role steps that return zero but fail to write the expected JSON")
    train.add_argument("--redo-roles", action="store_true",
                       help="force role subsessions to start over for every role step")
    train.set_defaults(func=cmd_train_batches)

    panel = sub.add_parser("build-validation-panel")
    panel.add_argument("--run-dir", type=Path, required=True)
    panel.add_argument("--through-batch", type=int, required=True)
    panel.add_argument("--out-dir", type=Path)
    panel.add_argument("--success-count", type=int, default=2)
    panel.add_argument("--crash-count", type=int, default=2)
    panel.add_argument("--false-positive-count", dest="crash_count", type=int, help=argparse.SUPPRESS)
    panel.add_argument("--risk-count", dest="crash_count", type=int, help=argparse.SUPPRESS)
    panel.add_argument("--failure-count", type=int, default=2)
    panel.add_argument("--panel-index", type=int)
    panel.set_defaults(func=cmd_build_validation_panel)

    run_test = sub.add_parser("run-test", help="run the held-out test split once with a frozen packet")
    run_test.add_argument("--run-dir", type=Path, required=True)
    run_test.add_argument("--arm", required=True, help="arm label, e.g. initial / distilled / bare")
    run_test.add_argument("--packet", type=Path)
    run_test.add_argument("--bare", action="store_true", help="strip every learned lesson from the packet")
    run_test.add_argument("--limit", type=int, default=0)
    _add_harness_args(run_test)
    run_test.set_defaults(func=cmd_run_test)

    eval_test = sub.add_parser("evaluate-test")
    eval_test.add_argument("--run-dir", type=Path, required=True)
    eval_test.add_argument("--arm", required=True)
    eval_test.add_argument("--results-dir", type=Path, required=True)
    eval_test.set_defaults(func=cmd_evaluate_test)

    freeze = sub.add_parser("freeze-split", help="pin the chronological split into gt_results (run once)")
    freeze.add_argument("--valid-gt", type=Path, default=REPO_ROOT / "gt_results" / "valid_gt.json")
    freeze.add_argument("--commit-dates", type=Path)
    freeze.add_argument("--require-dates", action="store_true",
                        help="refuse to freeze while a training sample still lacks a commit date")
    freeze.add_argument("--force", action="store_true", help="overwrite an existing frozen split")
    freeze.set_defaults(func=cmd_freeze_split)


    synth_ref = sub.add_parser("synthesize-reference", help="build a GT-assisted synthetic reference trajectory for distillation roles")
    synth_ref.add_argument("--sample-id", required=True)
    synth_ref.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_ROOT / "default")
    synth_ref.add_argument("--out-dir", type=Path)
    synth_ref.add_argument("--out-file", type=Path)
    synth_ref.set_defaults(func=cmd_synthesize_reference)

    audit = sub.add_parser("audit-plan")
    audit.set_defaults(func=cmd_audit_plan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
