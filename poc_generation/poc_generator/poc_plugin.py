#!/usr/bin/env python3
"""Config-driven launcher for PoC generation (the subject-under-test side).

A single JSON config (see poc_config.example.json) drives everything: the
backend/model the subject runs with, the iteration cap and early-death retry
budget, how many samples to run in parallel, and the sample id list. Start it
once and it generates PoCs (and the required final fine trace) for
the whole list unattended:

    python3 poc_generation/poc_generator/poc_plugin.py --config .../poc_config.json

It is a thin orchestrator over run_sample.py (one full generation episode per
sample, already handling the cybergym task, checkpoint/manifest/trace output to
poc_results/<id>/, and early-death re-runs). The launcher fans out across
`parallel` workers, frees each sample's ARVO Docker images afterward (so a batch
does not exhaust local disk), and writes a batch summary. Mirrors
gt_generation/gt_plugin.py for the GT side.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent                 # poc_generation/poc_generator/
REPO_ROOT = HERE.parents[1]                             # repo root
RUN_SAMPLE = HERE / "run_sample.py"
POC_RESULTS = HERE.parent / "poc_results"
MAX_PARALLEL = 6  # local Docker budget ceiling
SUPPORTED_BACKENDS = {"openhands"}  # codex/claude/coco not wired here yet


def load_config(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"config must be a JSON object: {path}")

    backend = str(raw.get("backend") or "openhands").strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        raise SystemExit(f"config.backend must be one of {sorted(SUPPORTED_BACKENDS)}; got {backend!r}")

    model = str(raw.get("model") or "").strip()
    if not model:
        raise SystemExit("config.model is required")

    parallel = int(raw.get("parallel") or 1)
    if not 1 <= parallel <= MAX_PARALLEL:
        raise SystemExit(f"config.parallel must be between 1 and {MAX_PARALLEL}")

    samples = raw.get("samples") or []
    if not isinstance(samples, list) or not samples:
        raise SystemExit("config.samples must be a non-empty list of sample ids")
    # normalise to bare numeric ARVO ids, de-duplicated in order
    arvo_ids = list(dict.fromkeys(str(s).removeprefix("arvo_") for s in samples))

    results_namespace = str(raw.get("results_namespace") or "").strip()
    if (
        not results_namespace
        or results_namespace in {".", ".."}
        or Path(results_namespace).name != results_namespace
    ):
        raise SystemExit(
            "config.results_namespace must be one non-empty directory name "
            "(for example 'deepseek-v4-flash')"
        )

    return {
        "backend": backend,
        "model": model,
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key_env": str(raw.get("api_key_env") or "").strip(),
        "max_iter": int(raw.get("max_iter") or 100),
        "max_attempts": int(raw.get("max_attempts") or 3),
        "openhands_repo": str(raw.get("openhands_repo") or "").strip(),
        "parallel": parallel,
        "server": str(raw.get("server") or "http://host.docker.internal:8666"),
        "difficulty": str(raw.get("difficulty") or "level1"),
        "results_namespace": results_namespace,
        "results_dir": POC_RESULTS / results_namespace,
        "arvo_ids": arvo_ids,
    }


def cleanup_arvo(arvo_id: str) -> None:
    """Drop the per-sample ARVO container/images after a run so parallel workers
    do not exhaust local disk."""
    subprocess.run(
        ["docker", "rm", "-f", f"gt-arvo_{arvo_id}-workspace"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for image in (f"n132/arvo:{arvo_id}-vul", f"n132/arvo:{arvo_id}-fix"):
        subprocess.run(
            ["docker", "image", "rm", image],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def run_one(arvo_id: str, cfg: dict[str, Any], logs_dir: Path,
            running: dict[str, float], running_lock: threading.Lock) -> dict[str, Any]:
    sample_id = f"arvo_{arvo_id}"
    log_path = logs_dir / f"{sample_id}.log"
    started = time.monotonic()
    with running_lock:
        running[sample_id] = started

    command = [
        sys.executable, str(RUN_SAMPLE),
        "--arvo-id", arvo_id,
        "--model", cfg["model"],
        "--base-url", cfg["base_url"],
        "--max-iter", str(cfg["max_iter"]),
        "--max-attempts", str(cfg["max_attempts"]),
        "--server", cfg["server"],
        "--difficulty", cfg["difficulty"],
        "--results-dir", str(cfg["results_dir"]),
    ]
    if cfg["openhands_repo"]:
        command += ["--openhands-repo", cfg["openhands_repo"]]
    if cfg["api_key_env"]:
        command += ["--api-key-env", cfg["api_key_env"]]
    with log_path.open("w", encoding="utf-8") as stream:
        completed = subprocess.run(command, cwd=HERE, stdout=stream, stderr=subprocess.STDOUT)

    cleanup_arvo(arvo_id)
    with running_lock:
        running.pop(sample_id, None)

    # Read back what the episode produced (run_sample writes these to poc_results).
    manifest = cfg["results_dir"] / sample_id / "manifest.json"
    trace = cfg["results_dir"] / sample_id / "fine_trace.json"
    status, poc_success = None, None
    if manifest.is_file():
        m = json.loads(manifest.read_text())
        status = m.get("status")
        poc_success = (m.get("poc_generation") or {}).get("success")
    result = {
        "sample_id": sample_id,
        "returncode": completed.returncode,
        "status": status,
        "poc_success": poc_success,
        "fine_trace_produced": trace.is_file(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": str(log_path),
    }
    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "poc_config.json",
                        help="PoC-generation config JSON (see poc_config.example.json).")
    parser.add_argument("--batch-name", default=datetime.now().strftime("poc_%Y%m%d_%H%M%S"))
    args = parser.parse_args(argv)

    if not args.config.is_file():
        raise SystemExit(f"config not found: {args.config} (copy poc_config.example.json to get started)")
    cfg = load_config(args.config)

    logs_dir = Path("/tmp") / f"poc_batch_{args.batch_name}" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    print(json.dumps({
        "batch": args.batch_name, "backend": cfg["backend"], "model": cfg["model"],
        "base_url": cfg["base_url"], "api_key_env": cfg["api_key_env"],
        "results_namespace": cfg["results_namespace"],
        "parallel": cfg["parallel"], "samples": len(cfg["arvo_ids"]),
        "max_iter": cfg["max_iter"], "max_attempts": cfg["max_attempts"],
    }, indent=2, ensure_ascii=False), flush=True)

    running: dict[str, float] = {}
    running_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=cfg["parallel"]) as executor:
        futures = {
            executor.submit(run_one, aid, cfg, logs_dir, running, running_lock)
            for aid in cfg["arvo_ids"]
        }
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=30)
            for future in done:
                results.append(future.result())
            if pending:
                with running_lock:
                    active = {s: round(time.monotonic() - t, 1) for s, t in running.items()}
                print("HEARTBEAT " + json.dumps(active, ensure_ascii=False), flush=True)

    order = {f"arvo_{aid}": i for i, aid in enumerate(cfg["arvo_ids"])}
    results.sort(key=lambda r: order.get(r["sample_id"], 0))
    summary = {
        "batch": args.batch_name, "backend": cfg["backend"], "model": cfg["model"],
        "base_url": cfg["base_url"], "api_key_env": cfg["api_key_env"],
        "results_namespace": cfg["results_namespace"],
        "requested": len(cfg["arvo_ids"]),
        "fine_traces_produced": sum(1 for r in results if r["fine_trace_produced"]),
        "pocs_succeeded": sum(1 for r in results if r["poc_success"]),
        "results": results,
    }
    summary_path = cfg["results_dir"] / f"batch_{args.batch_name}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    # Batch completeness requires every task to persist its final fine trace.
    return 0 if summary["fine_traces_produced"] == len(cfg["arvo_ids"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
