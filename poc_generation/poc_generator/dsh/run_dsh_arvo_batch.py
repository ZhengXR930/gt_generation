#!/usr/bin/env python3
"""Run DeepSeek Harness ARVO PoC-generation samples in parallel.

This is a narrow operational runner for host-side DSH evaluation.  It selects
ARVO samples with verified GT, skips already materialized DSH results, and runs
`run_deepseek_harness_arvo_sample.py` with a fixed sample-level step cap.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DSH_ROOT = Path(__file__).resolve().parent
GENERATOR_ROOT = DSH_ROOT.parent
sys.path.insert(0, str(GENERATOR_ROOT))

from dsh.reachability_pipeline import run_reachability_pipeline  # noqa: E402
from dsh.run_deepseek_harness_arvo_sample import cleanup_arvo_target_image  # noqa: E402


HERE = DSH_ROOT
GT_ROOT = GENERATOR_ROOT.parents[1]
RESULTS_ROOT = GENERATOR_ROOT.parent / "poc_results"
LOG_ROOT = RESULTS_ROOT / "_batch_logs"
RUN_ARVO = HERE / "run_deepseek_harness_arvo_sample.py"


COMPLETE_STATUSES = {"success", "iteration_cap", "agent_finished"}


def sample_sort_key(sample_id: str) -> tuple[int, str]:
    match = re.fullmatch(r"arvo_(\d+)", sample_id)
    if match:
        return int(match.group(1)), sample_id
    return 10**18, sample_id


def has_verified_arvo_gt(sample_dir: Path) -> bool:
    return (
        sample_dir.is_dir()
        and re.fullmatch(r"arvo_\d+", sample_dir.name) is not None
        and (sample_dir / "verified_assertions.json").is_file()
        and (sample_dir / "issue_description.json").is_file()
    )


def result_is_done(result_dir: Path) -> bool:
    manifest_path = result_dir / "manifest.json"
    if not manifest_path.is_file() or not (result_dir / "checkpoint").is_dir():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        manifest.get("max_iter") == 100
        and manifest.get("status") in COMPLETE_STATUSES
    )


def select_samples(namespace: str, limit: int, explicit_samples: list[str]) -> list[str]:
    if explicit_samples:
        return explicit_samples[:limit] if limit > 0 else explicit_samples
    candidates = sorted(
        (path.name for path in (GT_ROOT / "gt_results").iterdir() if has_verified_arvo_gt(path)),
        key=sample_sort_key,
    )
    result_root = RESULTS_ROOT / namespace
    selected: list[str] = []
    for sample_id in candidates:
        if result_is_done(result_root / sample_id):
            continue
        selected.append(sample_id)
        if limit > 0 and len(selected) >= limit:
            break
    return selected


def run_one(args: argparse.Namespace, sample_id: str) -> dict:
    namespace = args.namespace
    result_root = RESULTS_ROOT / namespace
    result_dir = result_root / sample_id
    if result_is_done(result_dir):
        return {"sample": sample_id, "status": "skipped"}

    log_dir = LOG_ROOT / namespace
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{sample_id}.log"
    arvo_id = sample_id.split("_", 1)[1]
    command = [
        sys.executable,
        str(RUN_ARVO),
        "--arvo-id",
        arvo_id,
        "--max-iter",
        str(args.max_iter),
        "--server",
        args.server,
        "--difficulty",
        args.difficulty,
        "--timeout",
        str(args.timeout),
        "--model",
        args.model,
        "--base-url",
        args.base_url,
        "--api-key-env",
        args.api_key_env,
        "--results-dir",
        str(result_root),
        "--max-attempts",
        str(args.max_attempts),
        "--dsh-home",
        str(args.dsh_home),
        "--scratch-root",
        str(args.scratch_root),
        "--reasoning-effort",
        args.reasoning_effort,
    ]
    if args.dsh_src:
        command.extend(["--dsh-src", str(args.dsh_src)])
    if args.node_root:
        command.extend(["--node-root", str(args.node_root)])
    if args.allow_tool_network:
        command.append("--allow-tool-network")
    # In normal batch mode the parent owns reachability and target-image
    # cleanup, so generation workers can free their 4-way slots immediately
    # after PoC generation finishes.  Standalone sample runner still supports
    # its own reachability+cleanup pipeline.
    if args.cleanup_target_image and not args.run_reachability_after_generation:
        command.append("--cleanup-target-image")
    command.append("--no-run-reachability-after-generation")

    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        returncode = subprocess.run(
            command,
            cwd=GT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        ).returncode
    seconds = round(time.monotonic() - started, 1)
    manifest_status = None
    submissions = None
    analysis_produced = None
    completed_steps = None
    manifest_path = result_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_status = manifest.get("status")
            submissions = manifest.get("num_submission_attempts")
            analysis_produced = (manifest.get("analysis") or {}).get("produced")
            completed_steps = (manifest.get("iteration_cap") or {}).get("completed")
            reachability = manifest.get("reachability")
        except (OSError, json.JSONDecodeError):
            reachability = None
    else:
        reachability = None
    return {
        "sample": sample_id,
        "status": "complete" if returncode == 0 else "failed",
        "returncode": returncode,
        "seconds": seconds,
        "manifest_status": manifest_status,
        "completed_steps": completed_steps,
        "num_submission_attempts": submissions,
        "analysis_produced": analysis_produced,
        "reachability_status": (
            reachability.get("status") if isinstance(reachability, dict) else None
        ),
        "reachability_summary": (
            reachability.get("summary") if isinstance(reachability, dict) else None
        ),
        "checkpoint": (result_dir / "checkpoint").is_dir(),
        "log": str(log_path),
    }


def run_reachability_and_cleanup(args: argparse.Namespace, sample_id: str) -> dict:
    """Run post-generation reachability, then clean the ARVO target image."""
    result_root = RESULTS_ROOT / args.namespace
    result_dir = result_root / sample_id
    arvo_id = sample_id.split("_", 1)[1]
    started = time.monotonic()
    reachability = run_reachability_pipeline(
        model_namespace=args.namespace,
        sample_id=sample_id,
        sample_result_dir=result_dir,
        enabled=args.run_reachability_after_generation,
        timeout=args.reachability_timeout,
        debugger_image=args.reachability_debugger_image,
        max_hits_per_event=args.reachability_max_hits_per_event,
        concurrency=args.reachability_concurrency,
        lock_dir=args.reachability_lock_dir.expanduser().resolve(),
    )
    cleanup = None
    if args.cleanup_target_image:
        cleanup = cleanup_arvo_target_image(arvo_id)
        cleanup_path = result_dir / "target_image_cleanup.json"
        cleanup_path.write_text(
            json.dumps(cleanup, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return {
        "sample": sample_id,
        "phase": "post_generation",
        "status": "complete",
        "seconds": round(time.monotonic() - started, 1),
        "reachability_status": reachability.get("status"),
        "reachability_summary": reachability.get("summary"),
        "reachability_reason": reachability.get("reason"),
        "reachability_error": reachability.get("error"),
        "target_image_cleanup": cleanup,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="deepseek-harness-v4-flash")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=10800)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--server", default="http://host.docker.internal:8666")
    parser.add_argument("--difficulty", default="level1")
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--reasoning-effort", default="max", choices=("off", "high", "max"))
    parser.add_argument("--dsh-home", type=Path, default=Path("/home/xinran/.cache/gt_generation_deepseek_harness_home"))
    parser.add_argument("--scratch-root", type=Path, default=Path("/home/xinran/.cache/gt_generation_dsh_scratch"))
    parser.add_argument("--dsh-src", type=Path, default=GT_ROOT / "external" / "deepseek-harness")
    parser.add_argument("--node-root", type=Path, default=Path("/home/xinran/.local/node-v24-musl"))
    parser.add_argument("--allow-tool-network", action="store_true")
    parser.add_argument("--cleanup-target-image", action="store_true", default=True)
    parser.add_argument("--no-cleanup-target-image", dest="cleanup_target_image", action="store_false")
    parser.add_argument(
        "--run-reachability-after-generation",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--no-run-reachability-after-generation",
        dest="run_reachability_after_generation",
        action="store_false",
    )
    parser.add_argument("--reachability-timeout", type=int, default=420)
    parser.add_argument("--reachability-debugger-image", default="gt-memory-env:latest")
    parser.add_argument("--reachability-max-hits-per-event", type=int, default=64)
    parser.add_argument("--reachability-concurrency", type=int, default=1)
    parser.add_argument(
        "--reachability-lock-dir",
        type=Path,
        default=Path("/home/xinran/.cache/gt_generation_reachability_locks"),
    )
    parser.add_argument("--samples", nargs="*", default=[])
    args = parser.parse_args()

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    batch_log_dir = LOG_ROOT / args.namespace
    batch_log_dir.mkdir(parents=True, exist_ok=True)
    selected = select_samples(args.namespace, args.limit, args.samples)
    selected_path = batch_log_dir / f"selected_{int(time.time())}.json"
    selected_path.write_text(
        json.dumps(
            {
                "namespace": args.namespace,
                "limit": args.limit,
                "parallel": args.parallel,
                "max_iter": args.max_iter,
                "run_reachability_after_generation": args.run_reachability_after_generation,
                "reachability_concurrency": args.reachability_concurrency,
                "selected": selected,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    summary_path = batch_log_dir / f"batch_{int(time.time())}.jsonl"
    print(
        json.dumps(
            {
                "event": "batch_start",
                "namespace": args.namespace,
                "selected_count": len(selected),
                "parallel": args.parallel,
                "summary": str(summary_path),
                "selected": str(selected_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    counts: dict[str, int] = {}
    post_counts: dict[str, int] = {}
    with summary_path.open("a", encoding="utf-8") as summary:
        with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as generation_executor, ThreadPoolExecutor(
            max_workers=max(1, args.reachability_concurrency)
        ) as reachability_executor:
            generation_futures = {
                generation_executor.submit(run_one, args, sample): sample
                for sample in selected
            }
            reachability_futures = {}
            for future in as_completed(generation_futures):
                sample = generation_futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001 - batch should keep running.
                    record = {
                        "sample": sample,
                        "phase": "generation",
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                record.setdefault("phase", "generation")
                counts[record["status"]] = counts.get(record["status"], 0) + 1
                line = json.dumps(record, ensure_ascii=False)
                print(line, flush=True)
                summary.write(line + "\n")
                summary.flush()
                if record.get("status") != "skipped":
                    reachability_futures[
                        reachability_executor.submit(
                            run_reachability_and_cleanup, args, sample
                        )
                    ] = sample
            for future in as_completed(reachability_futures):
                sample = reachability_futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001
                    record = {
                        "sample": sample,
                        "phase": "post_generation",
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                post_counts[record["status"]] = post_counts.get(record["status"], 0) + 1
                line = json.dumps(record, ensure_ascii=False)
                print(line, flush=True)
                summary.write(line + "\n")
                summary.flush()
    print(
        json.dumps(
            {"event": "batch_done", "counts": counts, "post_counts": post_counts},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
