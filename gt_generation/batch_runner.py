#!/usr/bin/env python3
"""Run several GT samples with a bounded number of isolated Claude pipelines."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "gt_generation" / "runner.py"
CLAUDE_ADAPTER = (
    ROOT / "gt_generation" / "adapters" / "claude_code" / "gt_agent_claude.sh"
)


def _load_selection(path: Path) -> dict[str, dict[str, Any]]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError(f"selection must be a JSON list: {path}")
    return {str(value["sample_id"]): value for value in values}


def _cleanup_arvo(sample_id: str) -> None:
    arvo_id = sample_id.removeprefix("arvo_")
    subprocess.run(
        ["docker", "rm", "-f", f"gt-arvo_{arvo_id}-workspace"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for image in (f"n132/arvo:{arvo_id}-vul", f"n132/arvo:{arvo_id}-fix"):
        subprocess.run(
            ["docker", "image", "rm", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _current_stage(result_dir: Path) -> str:
    path = result_dir / "gt_generation_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "starting"
    return str(state.get("current_stage") or "starting")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, default=ROOT / "dataset" / "selected_1000.json")
    parser.add_argument("--sample-id", action="append", required=True)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--complex-model", default="claude-opus-4-6")
    parser.add_argument("--batch-name", default=datetime.now().strftime("claude_%Y%m%d_%H%M%S"))
    args = parser.parse_args(argv)
    if args.max_workers < 1 or args.max_workers > 6:
        raise SystemExit("max-workers must be between 1 and 6 for the local Docker budget")

    selection = _load_selection(args.selection)
    sample_ids = list(dict.fromkeys(args.sample_id))
    missing = [sample_id for sample_id in sample_ids if sample_id not in selection]
    if missing:
        raise SystemExit(f"sample ids absent from selection: {missing}")

    batch_dir = Path("/tmp") / f"gt_batch_{args.batch_name}"
    inputs = batch_dir / "inputs"
    logs = batch_dir / "logs"
    inputs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    running: dict[str, float] = {}
    running_lock = threading.Lock()

    def run_one(sample_id: str) -> dict[str, Any]:
        sample = selection[sample_id]
        input_path = inputs / f"{sample_id}.json"
        input_path.write_text(
            json.dumps(sample, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        result_dir = ROOT / "gt_results" / sample_id
        log_path = logs / f"{sample_id}.log"
        started = time.monotonic()
        with running_lock:
            running[sample_id] = started
        env = {
            **os.environ,
            # runner.py imports gt_generation.gt_toolkit.compact_result once every
            # stage has passed, so the repo root must be importable. A script run
            # only puts runner.py's own directory on sys.path, which would make
            # that import fail after a full successful pipeline.
            "PYTHONPATH": os.pathsep.join(
                [str(ROOT), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
            ),
            "GT_AGENT_COMMAND": str(CLAUDE_ADAPTER),
            "GT_CLAUDE_MODEL": args.model,
            "GT_CLAUDE_COMPLEX_MODEL": args.complex_model,
        }
        command = [
            sys.executable,
            str(RUNNER),
            "--sample",
            str(input_path),
            "--result-dir",
            str(result_dir),
        ]
        with log_path.open("w", encoding="utf-8") as stream:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        _cleanup_arvo(sample_id)
        duration = round(time.monotonic() - started, 3)
        with running_lock:
            running.pop(sample_id, None)
        audit_ok = False
        if completed.returncode == 0:
            audit = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "gt_toolkit",
                    "audit-package",
                    "--result-dir",
                    str(result_dir),
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "gt_generation")},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            audit_ok = audit.returncode == 0
        result = {
            "sample_id": sample_id,
            "project": sample.get("project"),
            "vulnerability_class": sample.get("vulnerability_class"),
            "returncode": completed.returncode,
            "audit_ok": audit_ok,
            "duration_seconds": duration,
            "result_dir": str(result_dir),
            "log": str(log_path),
        }
        print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
        return result

    print(
        json.dumps(
            {
                "batch": args.batch_name,
                "samples": sample_ids,
                "max_workers": args.max_workers,
                "agent": "claude",
                "model": args.model,
                "complex_model": args.complex_model,
                "batch_dir": str(batch_dir),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = [executor.submit(run_one, sample_id) for sample_id in sample_ids]
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=30)
            for future in done:
                results.append(future.result())
            if pending:
                with running_lock:
                    active = {
                        sample_id: {
                            "elapsed_seconds": round(time.monotonic() - started, 1),
                            "stage": _current_stage(ROOT / "gt_results" / sample_id),
                        }
                        for sample_id, started in running.items()
                    }
                print("HEARTBEAT " + json.dumps(active, ensure_ascii=False), flush=True)

    results.sort(key=lambda item: sample_ids.index(item["sample_id"]))
    summary = {
        "batch": args.batch_name,
        "agent": "claude",
        "model": args.model,
        "complex_model": args.complex_model,
        "max_workers": args.max_workers,
        "requested": len(sample_ids),
        "succeeded": sum(item["returncode"] == 0 and item["audit_ok"] for item in results),
        "results": results,
    }
    summary_path = ROOT / "gt_results" / f"batch_{args.batch_name}.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["succeeded"] == len(sample_ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
