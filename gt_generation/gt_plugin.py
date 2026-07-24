#!/usr/bin/env python3
"""Config-driven launcher for fine-grained GT generation.

A single JSON config (see gt_config.example.json) drives everything: which agent
CLI to use (claude / codex / coco), the one model to run every stage with, how
many samples to generate in parallel, and the sample id list. Start it once and
it generates GT for the whole list unattended:

    python3 gt_generation/gt_plugin.py --config gt_generation/gt_config.json

It is a thin orchestrator: per sample it invokes runner.py (the proven stage
engine) with the selected adapter wired in via GT_AGENT_COMMAND, and lets
gt_toolkit/prepare.py route Docker (ARVO samples -> original n132/arvo:<id>
images; everything else -> the shared gt-memory-env image). The launcher never
touches Docker itself; it only makes the routing decision visible up front.
"""

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

CODE_ROOT = Path(__file__).resolve().parent          # gt_generation/
REPO_ROOT = CODE_ROOT.parent                          # repo root
RUNNER = CODE_ROOT / "runner.py"

# cli name -> adapter script honoring runner.py's GT_AGENT_COMMAND contract
# (--role-file / --sample / --result-dir).
ADAPTERS = {
    "claude": CODE_ROOT / "adapters" / "claude_code" / "gt_agent_claude.sh",
    "codex": CODE_ROOT / "adapters" / "codex" / "gt_agent_codex.sh",
    "coco": CODE_ROOT / "adapters" / "coco" / "gt_agent_coco.sh",
}
MAX_PARALLEL = 6  # local Docker budget ceiling


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict[str, Any]:
    """Read and validate the launcher config, applying defaults."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"config must be a JSON object: {path}")

    cli = str(raw.get("cli") or "").strip().lower()
    if cli not in ADAPTERS:
        raise SystemExit(f"config.cli must be one of {sorted(ADAPTERS)}; got {cli!r}")
    adapter = ADAPTERS[cli]
    if not adapter.is_file():
        raise SystemExit(f"adapter for cli {cli!r} not found: {adapter}")

    model = str(raw.get("model") or "").strip()
    if not model:
        raise SystemExit("config.model is required (one model id for every stage)")

    parallel = int(raw.get("parallel_dockers") or 1)
    if not 1 <= parallel <= MAX_PARALLEL:
        raise SystemExit(f"config.parallel_dockers must be between 1 and {MAX_PARALLEL}")

    samples = raw.get("samples") or []
    if not isinstance(samples, list) or not samples:
        raise SystemExit("config.samples must be a non-empty list of sample ids")
    # de-duplicate while preserving order
    samples = list(dict.fromkeys(str(s) for s in samples))

    selection = str(raw.get("selection") or "dataset/selected_1000.json")
    selection_path = Path(selection)
    if not selection_path.is_absolute():
        selection_path = REPO_ROOT / selection_path

    return {
        "cli": cli,
        "adapter": adapter,
        "model": model,
        "parallel_dockers": parallel,
        "samples": samples,
        "selection_path": selection_path,
    }


def load_selection(path: Path) -> dict[str, dict[str, Any]]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise SystemExit(f"selection must be a JSON list: {path}")
    return {str(v["sample_id"]): v for v in values if isinstance(v, dict) and v.get("sample_id")}


# --------------------------------------------------------------------------- #
# Docker routing (decision only -- prepare.py does the actual work)            #
# --------------------------------------------------------------------------- #
def docker_track(sample: dict[str, Any]) -> str:
    """'arvo' (original n132/arvo:<id> images) or 'repo' (shared gt-memory-env),
    using the same signal prepare.py routes on, so the printed plan matches what
    stage 00 will actually do."""
    try:
        from gt_toolkit.prepare import _is_arvo  # type: ignore
        return "arvo" if _is_arvo(sample) else "repo"
    except Exception:
        is_arvo = bool(
            sample.get("arvo_image_vul")
            or str(sample.get("source_dataset", "")).upper().startswith("ARVO")
        )
        return "arvo" if is_arvo else "repo"


def cleanup_arvo(sample_id: str) -> None:
    """Drop the per-sample ARVO container/images after a run so parallel workers
    do not exhaust local disk. No-op for repo-track samples."""
    arvo_id = sample_id.removeprefix("arvo_")
    subprocess.run(
        ["docker", "rm", "-f", f"gt-arvo_{arvo_id}-workspace"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for image in (f"n132/arvo:{arvo_id}-vul", f"n132/arvo:{arvo_id}-fix"):
        subprocess.run(
            ["docker", "image", "rm", image],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def current_stage(result_dir: Path) -> str:
    try:
        state = json.loads((result_dir / "gt_generation_state.json").read_text(encoding="utf-8"))
    except Exception:
        return "starting"
    return str(state.get("current_stage") or "starting")


# --------------------------------------------------------------------------- #
# Run                                                                          #
# --------------------------------------------------------------------------- #
def run_one(sample_id: str, sample: dict[str, Any], cfg: dict[str, Any],
            inputs_dir: Path, logs_dir: Path, running: dict[str, float],
            running_lock: threading.Lock) -> dict[str, Any]:
    input_path = inputs_dir / f"{sample_id}.json"
    input_path.write_text(json.dumps(sample, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    result_dir = REPO_ROOT / "gt_results" / sample_id
    log_path = logs_dir / f"{sample_id}.log"

    started = time.monotonic()
    with running_lock:
        running[sample_id] = started

    env = {
        **os.environ,
        # runner.py imports gt_toolkit from the repo root after all stages pass.
        "PYTHONPATH": os.pathsep.join(
            [str(REPO_ROOT), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
        ),
        "GT_AGENT_COMMAND": str(cfg["adapter"]),
        "GT_AGENT_MODEL": cfg["model"],
    }
    command = [
        sys.executable, str(RUNNER),
        "--sample", str(input_path),
        "--result-dir", str(result_dir),
    ]
    with log_path.open("w", encoding="utf-8") as stream:
        completed = subprocess.run(command, cwd=REPO_ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)

    if docker_track(sample) == "arvo":
        cleanup_arvo(sample_id)

    with running_lock:
        running.pop(sample_id, None)

    # Final deterministic gate: the packaged GT passes audit-package.
    audit_ok = False
    if completed.returncode == 0:
        audit = subprocess.run(
            [sys.executable, "-m", "gt_toolkit", "audit-package", "--result-dir", str(result_dir)],
            cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(CODE_ROOT)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        audit_ok = audit.returncode == 0

    result = {
        "sample_id": sample_id,
        "track": docker_track(sample),
        "project": sample.get("project"),
        "returncode": completed.returncode,
        "audit_ok": audit_ok,
        "duration_seconds": round(time.monotonic() - started, 3),
        "result_dir": str(result_dir),
        "log": str(log_path),
    }
    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CODE_ROOT / "gt_config.json",
                        help="Launcher config JSON (see gt_config.example.json).")
    parser.add_argument("--batch-name", default=datetime.now().strftime("gt_%Y%m%d_%H%M%S"))
    args = parser.parse_args(argv)

    if not args.config.is_file():
        raise SystemExit(f"config not found: {args.config} (copy gt_config.example.json to get started)")
    cfg = load_config(args.config)
    selection = load_selection(cfg["selection_path"])

    missing = [s for s in cfg["samples"] if s not in selection]
    if missing:
        raise SystemExit(f"sample ids absent from selection {cfg['selection_path']}: {missing}")

    batch_dir = Path("/tmp") / f"gt_batch_{args.batch_name}"
    inputs_dir = batch_dir / "inputs"
    logs_dir = batch_dir / "logs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Routing plan: make the ARVO-vs-repo Docker decision visible before running.
    plan = {s: docker_track(selection[s]) for s in cfg["samples"]}
    print(json.dumps({
        "batch": args.batch_name,
        "cli": cfg["cli"],
        "model": cfg["model"],
        "parallel_dockers": cfg["parallel_dockers"],
        "samples": len(cfg["samples"]),
        "docker_routing": {
            "arvo (n132/arvo images)": sorted(s for s, t in plan.items() if t == "arvo"),
            "repo (gt-memory-env)": sorted(s for s, t in plan.items() if t == "repo"),
        },
        "batch_dir": str(batch_dir),
    }, indent=2, ensure_ascii=False), flush=True)

    running: dict[str, float] = {}
    running_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=cfg["parallel_dockers"]) as executor:
        futures = {
            executor.submit(run_one, s, selection[s], cfg, inputs_dir, logs_dir, running, running_lock)
            for s in cfg["samples"]
        }
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=30)
            for future in done:
                results.append(future.result())
            if pending:
                with running_lock:
                    active = {
                        s: {"elapsed_seconds": round(time.monotonic() - t, 1),
                            "stage": current_stage(REPO_ROOT / "gt_results" / s)}
                        for s, t in running.items()
                    }
                print("HEARTBEAT " + json.dumps(active, ensure_ascii=False), flush=True)

    results.sort(key=lambda item: cfg["samples"].index(item["sample_id"]))
    succeeded = sum(r["returncode"] == 0 and r["audit_ok"] for r in results)
    summary = {
        "batch": args.batch_name,
        "cli": cfg["cli"],
        "model": cfg["model"],
        "parallel_dockers": cfg["parallel_dockers"],
        "requested": len(cfg["samples"]),
        "succeeded": succeeded,
        "results": results,
    }
    summary_path = REPO_ROOT / "gt_results" / f"batch_{args.batch_name}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if succeeded == len(cfg["samples"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
