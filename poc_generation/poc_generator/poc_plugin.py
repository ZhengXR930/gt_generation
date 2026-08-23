#!/usr/bin/env python3
"""Config-driven launcher for PoC generation.

This is the subject-under-test side of evaluation.  It owns batching, concurrency,
logs, result namespaces, and cleanup.  It does not know how to launch a concrete
agent harness; that is delegated to ``reward_framework.adapters.poc_generation``.

Normal baseline evaluation writes to ``poc_generation/poc_results`` and never
loads reward-framework skill packets.  Skill evolution/validation must use the
reward-framework entrypoints and pass an explicit adapter-owned result root.
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

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
POC_RESULTS = HERE.parent / "poc_results"
MAX_PARALLEL = 6  # local Docker / workspace budget ceiling

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reward_framework.adapters.poc_generation import (  # noqa: E402
    PocHarnessRequest,
    build_poc_harness_command,
    normalize_harness_name,
    supported_harnesses,
)


def load_config(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"config must be a JSON object: {path}")

    backend = normalize_harness_name(raw.get("harness") or raw.get("backend") or "openhands")
    if backend not in supported_harnesses():
        raise SystemExit(
            f"config.backend/config.harness must be one of {list(supported_harnesses())}; got {backend!r}"
        )

    # Keep poc_generation as the baseline/evaluation path.  Reward-framework
    # skill experiments are intentionally isolated under reward_framework/*.
    if str(raw.get("skill_packet_dir") or raw.get("openhands_skill_packet_dir") or "").strip():
        raise SystemExit(
            "poc_generation baseline configs must not load skill_packet_dir. "
            "Use reward_framework adapter entrypoints for skill runs."
        )

    model = str(raw.get("model") or "").strip()
    if not model:
        raise SystemExit("config.model is required")

    parallel = int(raw.get("parallel") or 1)
    if not 1 <= parallel <= MAX_PARALLEL:
        raise SystemExit(f"config.parallel must be between 1 and {MAX_PARALLEL}")

    samples = raw.get("samples") or []
    if not isinstance(samples, list) or not samples:
        raise SystemExit("config.samples must be a non-empty list of ARVO sample ids")
    arvo_ids = []
    for raw_sample in samples:
        value = str(raw_sample)
        arvo_ids.append(value[len("arvo_") :] if value.startswith("arvo_") else value)
    arvo_ids = list(dict.fromkeys(arvo_ids))

    results_namespace = str(raw.get("results_namespace") or "").strip()
    if (
        not results_namespace
        or results_namespace in {".", ".."}
        or Path(results_namespace).name != results_namespace
    ):
        raise SystemExit(
            "config.results_namespace must be one non-empty directory name "
            "(for example 'deepseek-v4-flash' or 'openhands-gpt54-mini')"
        )

    openhands_repo_raw = str(raw.get("openhands_repo") or "").strip()
    return {
        "backend": backend,
        "harness": backend,
        "model": model,
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key_env": str(raw.get("api_key_env") or "").strip(),
        "api_version": str(raw.get("api_version") or "").strip(),
        "max_iter": int(raw.get("max_iter") or 100),
        "max_attempts": int(raw.get("max_attempts") or 3),
        "timeout": int(raw.get("timeout") or 10800),
        "tmp_root": str(raw.get("tmp_root") or "").strip(),
        "openhands_repo": Path(openhands_repo_raw).expanduser() if openhands_repo_raw else None,
        "parallel": parallel,
        "server": str(raw.get("server") or "http://host.docker.internal:8666"),
        "difficulty": str(raw.get("difficulty") or "level1"),
        "results_namespace": results_namespace,
        "results_dir": POC_RESULTS / results_namespace,
        "arvo_ids": arvo_ids,
    }


def cleanup_arvo(arvo_id: str) -> None:
    """Drop rebuildable per-sample ARVO containers/images after a run.

    This keeps long batches from accumulating target images.  The source tree is
    materialized on demand by the sample runner when needed.
    """
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


def _read_manifest(cfg: dict[str, Any], sample_id: str) -> tuple[str | None, bool | None, bool]:
    manifest = cfg["results_dir"] / sample_id / "manifest.json"
    analysis = cfg["results_dir"] / sample_id / "analysis.json"
    status, poc_success = None, None
    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        status = payload.get("status")
        poc_success = (payload.get("poc_generation") or {}).get("success")
    return status, poc_success, analysis.is_file()


def run_one(
    arvo_id: str,
    cfg: dict[str, Any],
    logs_dir: Path,
    running: dict[str, float],
    running_lock: threading.Lock,
) -> dict[str, Any]:
    sample_id = f"arvo_{arvo_id}"
    log_path = logs_dir / f"{sample_id}.log"
    started = time.monotonic()
    with running_lock:
        running[sample_id] = started

    request = PocHarnessRequest(
        harness=cfg["harness"],
        arvo_id=arvo_id,
        model=cfg["model"],
        base_url=cfg["base_url"],
        api_key_env=cfg["api_key_env"],
        api_version=cfg["api_version"],
        max_iter=cfg["max_iter"],
        max_attempts=cfg["max_attempts"],
        timeout=cfg["timeout"],
        server=cfg["server"],
        difficulty=cfg["difficulty"],
        results_dir=cfg["results_dir"],
        openhands_repo=cfg["openhands_repo"],
        skill_packet_dir=None,
    )
    adapter_command = build_poc_harness_command(request)
    with log_path.open("w", encoding="utf-8") as stream:
        stream.write("ADAPTER_COMMAND " + json.dumps(adapter_command.redacted(), ensure_ascii=False) + "\n")
        stream.flush()
        command_env = dict(adapter_command.env)
        if cfg["tmp_root"]:
            Path(cfg["tmp_root"]).mkdir(parents=True, exist_ok=True)
            command_env["TMPDIR"] = cfg["tmp_root"]
        completed = subprocess.run(
            adapter_command.command,
            cwd=adapter_command.cwd,
            env=command_env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )

    cleanup_arvo(arvo_id)
    with running_lock:
        running.pop(sample_id, None)

    status, poc_success, analysis_produced = _read_manifest(cfg, sample_id)
    result = {
        "sample_id": sample_id,
        "harness": cfg["harness"],
        "model": cfg["model"],
        "returncode": completed.returncode,
        "status": status,
        "poc_success": poc_success,
        "analysis_produced": analysis_produced,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": str(log_path),
    }
    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=HERE / "poc_config.json",
        help="PoC-generation config JSON (see poc_config.example.json).",
    )
    parser.add_argument("--batch-name", default=datetime.now().strftime("poc_%Y%m%d_%H%M%S"))
    args = parser.parse_args(argv)

    if not args.config.is_file():
        raise SystemExit(f"config not found: {args.config} (copy poc_config.example.json to get started)")
    cfg = load_config(args.config)

    logs_dir = Path("/tmp") / f"poc_batch_{args.batch_name}" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    print(
        json.dumps(
            {
                "batch": args.batch_name,
                "harness": cfg["harness"],
                "model": cfg["model"],
                "base_url_configured": bool(cfg["base_url"]),
                "api_key_env": cfg["api_key_env"],
                "results_namespace": cfg["results_namespace"],
                "parallel": cfg["parallel"],
                "samples": len(cfg["arvo_ids"]),
                "max_iter": cfg["max_iter"],
                "max_attempts": cfg["max_attempts"],
                "timeout": cfg["timeout"],
            },
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

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
        "batch": args.batch_name,
        "harness": cfg["harness"],
        "model": cfg["model"],
        "base_url_configured": bool(cfg["base_url"]),
        "api_key_env": cfg["api_key_env"],
        "results_namespace": cfg["results_namespace"],
        "requested": len(cfg["arvo_ids"]),
        "analysis_artifacts_produced": sum(1 for r in results if r["analysis_produced"]),
        "pocs_succeeded": sum(1 for r in results if r["poc_success"]),
        "results": results,
    }
    summary_path = cfg["results_dir"] / f"batch_{args.batch_name}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["analysis_artifacts_produced"] == len(cfg["arvo_ids"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
