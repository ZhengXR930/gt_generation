#!/usr/bin/env python3
"""Run the CyberGym/ARVO GT pipeline serially, one sample at a time."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
# gt_toolkit lives under gt_generation/; put it on PYTHONPATH so `-m gt_toolkit` resolves.
os.environ["PYTHONPATH"] = str(ROOT / "gt_generation") + os.pathsep + os.environ.get("PYTHONPATH", "")
DEFAULT_SELECTION = ROOT / "selected_samples_json" / "cybergym_overlap_50.json"
DEFAULT_RESULTS = ROOT / "gt_results"
DEFAULT_WORK = ROOT / "work" / "cybergym_arvo50"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(cmd: list[str], *, timeout: int | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout


def checked(cmd: list[str], *, timeout: int | None = None) -> str:
    code, out = run(cmd, timeout=timeout)
    if code != 0:
        raise RuntimeError(f"command failed ({code}): {' '.join(cmd)}\n{out[-6000:]}")
    return out


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def is_complete(results: Path, sample_id: str) -> bool:
    gt = results / sample_id / "ground_truth.json"
    state = load_state(results / sample_id / "sample_state.json")
    if not gt.exists() or state.get("status") != "gt_completed_validated":
        return False
    code, _ = run([
        "python3",
        "-m", "gt_toolkit", "validate",
        str(gt),
    ], timeout=300)
    return code == 0


def update_success_state(result_dir: Path, sid: str, schema: dict[str, Any], cleanup_done: bool) -> None:
    state_path = result_dir / "sample_state.json"
    state = load_state(state_path)
    completed = list(dict.fromkeys((state.get("completed_stages") or []) + [
        "draft_gt_generation",
        "deterministic_grounding",
        "schema_validation",
        "work_cleanup",
    ]))
    state.update({
        "sample_id": sid,
        "status": "gt_completed_validated",
        "current_stage": "completed",
        "completed_stages": completed,
        "failure": None,
        "updated_at": now(),
    })
    artifacts = state.setdefault("artifacts", {})
    artifacts.update({
        "ground_truth": "ground_truth.json",
        "watchpoint_json": "watchpoint.json",
    })
    artifacts.pop("schema_validation", None)
    artifacts.pop("sanitizer_grounding_smoke", None)
    validation = state.setdefault("validation", {})
    validation.update({
        "schema_valid": bool(schema.get("ok")),
        "schema_errors": schema.get("errors", []),
        "audit_valid": True,
        "requires_human_review": False,
        "review_reason": None,
    })
    cleanup = state.setdefault("cleanup", {})
    cleanup.update({"source_deleted": cleanup_done, "build_deleted": cleanup_done})
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def update_failure_state(result_dir: Path, sid: str, exc: Exception, stage: str) -> None:
    state_path = result_dir / "sample_state.json"
    state = load_state(state_path)
    state.update({
        "sample_id": sid,
        "status": "failed",
        "current_stage": stage,
        "failure": {"type": type(exc).__name__, "message": str(exc)},
        "updated_at": now(),
    })
    validation = state.setdefault("validation", {})
    validation.update({"requires_human_review": True, "review_reason": f"{stage} failed"})
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def append_log(result_dir: Path, message: str) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    with (result_dir / "generation.log").open("a") as fh:
        fh.write(message.rstrip() + "\n")


def clean_sample_work(work: Path, sid: str) -> None:
    shutil.rmtree(work / sid, ignore_errors=True)


def clean_arvo_images() -> None:
    code, out = run(["bash", "-lc", "docker images --format '{{.Repository}}:{{.Tag}}' | rg '^n132/arvo:' || true"], timeout=120)
    if code != 0:
        return
    images = [line.strip() for line in out.splitlines() if line.strip()]
    for image in images:
        run(["docker", "rmi", image], timeout=300)


def clean_result_intermediates(result_dir: Path) -> None:
    for name in ("sanitizer_grounding_smoke.json", "schema_validation.json"):
        path = result_dir / name
        if path.exists():
            path.unlink()


def process(record: dict[str, Any], args: argparse.Namespace) -> bool:
    sid = record["local_sample_id"]
    result_dir = args.results / sid
    append_log(result_dir, f"{now()} batch_start {sid}")
    stage = "materialize_and_reproduce"
    try:
        checked([
            "python3",
            "scripts/run_cybergym_arvo_serial.py",
            "--selection",
            str(args.selection),
            "--results",
            str(args.results),
            "--work",
            str(args.work),
            "--start-at",
            sid,
            "--limit",
            "1",
            "--copy-src",
            "--run-timeout",
            str(args.run_timeout),
        ], timeout=args.sample_timeout)

        stage = "draft_gt_generation"
        checked([
            "python3",
            "scripts/generate_arvo_gt_from_smoke.py",
            sid,
            "--results",
            str(args.results),
            "--work",
            str(args.work),
        ], timeout=300)

        stage = "deterministic_grounding"
        checked([
            "python3",
            "scripts/compute_grounding.py",
            str(result_dir / "ground_truth.json"),
            "--patch",
            str(result_dir / "patch.diff"),
            "--watchpoint",
            str(result_dir / "watchpoint.json"),
            "--in-place",
        ], timeout=300)

        stage = "schema_validation"
        schema_out = checked([
            "python3",
            "-m", "gt_toolkit", "validate",
            str(result_dir / "ground_truth.json"),
        ], timeout=300)
        schema = json.loads(schema_out)
        if not schema.get("ok"):
            raise RuntimeError(f"schema validation failed: {schema.get('errors')}")

        stage = "deterministic_audit"
        from scripts.audit_arvo_gt_correctness import audit_sample

        audit = audit_sample(record, args.results)
        if audit.get("status") != "pass":
            raise RuntimeError(f"deterministic audit failed: {json.dumps(audit, ensure_ascii=False)[:3000]}")

        stage = "cleanup"
        clean_sample_work(args.work, sid)
        clean_result_intermediates(result_dir)
        clean_arvo_images()
        update_success_state(result_dir, sid, schema, cleanup_done=True)
        append_log(result_dir, f"{now()} batch_completed {sid}")
        return True
    except Exception as exc:
        append_log(result_dir, f"{now()} batch_failed {sid} stage={stage}: {exc}")
        update_failure_state(result_dir, sid, exc, stage)
        clean_sample_work(args.work, sid)
        clean_arvo_images()
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--start-at", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run-timeout", type=int, default=300)
    parser.add_argument("--sample-timeout", type=int, default=5400)
    parser.add_argument("--rerun-complete", action="store_true")
    args = parser.parse_args()
    args.selection = args.selection.resolve()
    args.results = args.results.resolve()
    args.work = args.work.resolve()

    records = json.loads(args.selection.read_text())
    if args.start_at:
        idx = next((i for i, record in enumerate(records) if record["local_sample_id"] == args.start_at), None)
        if idx is None:
            raise SystemExit(f"start sample not found: {args.start_at}")
        records = records[idx:]
    if args.limit:
        records = records[: args.limit]

    total = len(records)
    attempted = 0
    succeeded = 0
    skipped = 0
    for index, record in enumerate(records, 1):
        sid = record["local_sample_id"]
        if not args.rerun_complete and is_complete(args.results, sid):
            skipped += 1
            print(f"[{index}/{total}] skip complete {sid}", flush=True)
            continue
        attempted += 1
        print(f"[{index}/{total}] run {sid} {record.get('project')} {record.get('cwe')}", flush=True)
        if process(record, args):
            succeeded += 1
            print(f"[{index}/{total}] ok {sid}", flush=True)
        else:
            print(f"[{index}/{total}] failed {sid}", flush=True)
    print(json.dumps({"total": total, "skipped": skipped, "attempted": attempted, "succeeded": succeeded}, indent=2), flush=True)


if __name__ == "__main__":
    main()
