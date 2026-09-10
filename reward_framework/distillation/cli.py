#!/usr/bin/env python3
"""Batch skill distillation workflow for reward-framework."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
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
from reward_framework.distillation.diagnosis_view import BEHAVIOR_SECTIONS, learning_diagnosis, learning_evaluation  # noqa: E402
from reward_framework.distillation.defaults import (  # noqa: E402
    DEFAULT_CODING_API_KEY_ENV,
    DEFAULT_CODING_BASE_URL,
    DEFAULT_CODING_HARNESS,
    DEFAULT_CODING_MODEL,
    DEFAULT_DISTILLER_API_KEY_ENV,
    DEFAULT_DISTILLER_BASE_URL,
    DEFAULT_DISTILLER_MODEL,
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
    split = load_frozen_split()
    write_split_manifest(run_dir / "split_manifest.json", split)
    copy_initial_packet(run_dir / "skill_packets" / "batch_000", args.initial_packet)
    write_json(run_dir / POOLS_FILE, load_pools(run_dir / POOLS_FILE))
    write_json(run_dir / "run_config.json", {
        "coding_agent": {"harness": args.coding_harness, "model": args.coding_model},
        "distiller": {"model": args.distiller_model, "base_url": args.base_url, "api_key_env": args.api_key_env},
    })
    print(json.dumps({
        "status": "initialized",
        "run_dir": str(run_dir),
        "ordering": split.ordering,
        "train": len(split.train),
        "test": len(split.test),
        "batches": len(split.batches),
        "undated_samples": len(split.undated),
    }, indent=2))
    return 0


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
        model=args.role_model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        api_version=args.api_version,
        reasoning_effort=args.reasoning_effort,
        timeout=args.role_timeout,
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
    for section in (*BEHAVIOR_SECTIONS, "retry_recommendation"):
        for text in _diagnosis_text_values(diagnosis.get(section)):
            for pattern in _FORBIDDEN_DIAGNOSIS_TERMS:
                for match in pattern.finditer(text):
                    hits.add(match.group(0))
    return sorted(hits)


def _retry_recommendation_error(diagnosis: dict[str, Any]) -> str | None:
    retry = diagnosis.get("retry_recommendation")
    if not isinstance(retry, dict):
        return "diagnosis missing retry_recommendation"
    decision = str(retry.get("decision") or "").strip()
    if decision not in {"retry", "do_not_retry"}:
        return "diagnosis retry_recommendation.decision must be retry or do_not_retry"
    missing = [
        f"retry_recommendation.{field}"
        for field in ("summary", "evidence")
        if not str(retry.get(field) or "").strip()
    ]
    if missing:
        return "diagnosis missing required retry field(s): " + ", ".join(missing)
    return None


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
    for section in BEHAVIOR_SECTIONS:
        value = diagnosis.get(section)
        if not isinstance(value, dict):
            missing.append(section)
            continue
        for field in ("summary", "evidence"):
            if not str(value.get(field) or "").strip():
                missing.append(f"{section}.{field}")
    if missing:
        return "diagnosis missing required behavior field(s): " + ", ".join(missing)
    retry_error = _retry_recommendation_error(diagnosis)
    if retry_error:
        return retry_error
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
    for section in BEHAVIOR_SECTIONS:
        if section in diagnosis:
            normalized[section] = diagnosis.get(section)
    if "retry_recommendation" in diagnosis:
        normalized["retry_recommendation"] = diagnosis.get("retry_recommendation")
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
            **{section: learning.get(section) for section in BEHAVIOR_SECTIONS},
            "retry_recommendation": learning.get("retry_recommendation"),
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
    window_size = max(1, int(window_size or 1))
    start = max(0, batch_index - window_size + 1)
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


def cmd_propose_updates(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    batch_dir = run_dir / f"batch_{args.batch_index:03d}"
    packet = _packet_for(run_dir, args.batch_index, args.skill_packet)
    if args.diagnoses:
        diagnoses = read_json(args.diagnoses.resolve())
        if isinstance(diagnoses, dict):
            diagnoses = diagnoses.get("diagnoses", diagnoses)
    else:
        diagnoses = _diagnoses_for_window(run_dir, args.batch_index, args.diagnosis_window_size)
    diagnoses = [learning_diagnosis(item) for item in diagnoses if isinstance(item, dict)]
    pools_view = teacher_view(load_pools(run_dir / POOLS_FILE), through_batch=args.batch_index)
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
    out_json = batch_dir / "curator_decisions.json"
    workspace = batch_dir / "subsessions" / "curator"
    status = _run_role(
        args, "curator", workspace, out_json=out_json,
        build=lambda: build_curator_role(workspace, packet, updates),
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
    pools_view = teacher_view(load_pools(run_dir / POOLS_FILE), through_batch=args.batch_index)
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
    applied_path = args.applied_updates.resolve() if args.applied_updates else run_dir / f"batch_{max(args.batch_index - 1, 0):03d}" / "applied_updates.json"
    applied_updates = read_json(applied_path) if applied_path.is_file() else {}
    out_packet = args.out_packet or run_dir / "skill_packets" / f"batch_{args.batch_index + 1:03d}"
    result = apply_correction_decision(
        current_packet,
        previous_packet,
        correction,
        out_packet=out_packet,
        allowed_lesson_ids=_applied_lesson_ids(applied_updates),
    )
    write_json(batch_dir / "correction_applied.json", result)
    print(json.dumps({
        "packet": result["packet"],
        "decision": result["decision"],
        "applied": result["applied"],
        "skipped": result["skipped"],
    }, indent=2, ensure_ascii=False))
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
    """Roles run as Codex subsessions on the same harness as the coding agent."""
    parser.add_argument("--execute", action="store_true",
                        help="run the role subsession; without it only the role workspace is prepared")
    parser.add_argument("--redo", action="store_true",
                        help="start every role over, discarding artifacts an earlier run produced")
    parser.add_argument("--role-model", default=DEFAULT_DISTILLER_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_DISTILLER_BASE_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_DISTILLER_API_KEY_ENV)
    parser.add_argument("--api-version", default="2024-03-01-preview")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--role-timeout", type=int, default=3600)


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
    init.add_argument("--coding-harness", default=DEFAULT_CODING_HARNESS)
    init.add_argument("--coding-model", default=DEFAULT_CODING_MODEL)
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
