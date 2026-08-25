#!/usr/bin/env python3
"""Sample-local reachability pipeline for PoC generation runners.

The PoC generation runner owns the target Docker image lifetime.  This module
runs reachability immediately after submitted PoCs and analysis artifacts have
been persisted, before the runner removes per-sample target images.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
RUNTIME_ROOT = HERE.parent
GT_ROOT = RUNTIME_ROOT.parent

sys.path.insert(0, str(GT_ROOT / "evaluator"))

from reachability.eval_batch import evaluate_model_sample  # noqa: E402


DEFAULT_REACHABILITY_LOCK_DIR = Path(
    os.environ.get(
        "GT_GENERATION_REACHABILITY_LOCK_DIR",
        str(Path.home() / ".cache" / "gt_generation_reachability_locks"),
    )
)


def sample_has_reachability_input(sample_result_dir: Path) -> tuple[bool, str]:
    """Return whether this result contains at least one persisted PoC candidate."""
    manifest_path = sample_result_dir / "manifest.json"
    if not manifest_path.is_file():
        return False, "missing_manifest"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid_manifest: {exc}"
    runtime_readiness = manifest.get("runtime_readiness")
    if (
        isinstance(runtime_readiness, dict)
        and runtime_readiness.get("runtime_spec_ready") is False
    ):
        return False, "unavailable_runtime_spec"
    poc_generation = manifest.get("poc_generation")
    if (
        isinstance(poc_generation, dict)
        and poc_generation.get("runtime_unavailable") is True
    ):
        return False, "unavailable_runtime_spec"
    candidates = manifest.get("deduplicated_pocs")
    if not isinstance(candidates, list) or not candidates:
        return False, "no_deduplicated_pocs"
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        poc_path = candidate.get("representative_poc_path")
        if poc_path and (sample_result_dir / str(poc_path)).is_file():
            return True, "has_poc"
    return False, "no_poc_file"


@contextmanager
def reachability_slot(lock_dir: Path, concurrency: int) -> Iterator[dict[str, Any]]:
    """Acquire one global reachability slot across all sample runner processes."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    slots = max(1, int(concurrency))
    started = time.monotonic()
    while True:
        for slot in range(slots):
            path = lock_dir / f"reachability.{slot}.lock"
            handle = path.open("w", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle.write(str(time.time()) + "\n")
                handle.flush()
                try:
                    yield {
                        "slot": slot,
                        "lock_path": str(path),
                        "wait_seconds": round(time.monotonic() - started, 3),
                        "concurrency": slots,
                    }
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                return
            except BlockingIOError:
                handle.close()
                continue
        time.sleep(2.0)


def update_manifest_reachability(sample_result_dir: Path, metadata: dict[str, Any]) -> None:
    manifest_path = sample_result_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    manifest["reachability"] = metadata
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def run_reachability_pipeline(
    *,
    model_namespace: str,
    sample_id: str,
    sample_result_dir: Path,
    enabled: bool,
    timeout: int,
    debugger_image: str,
    max_hits_per_event: int,
    concurrency: int,
    lock_dir: Path = DEFAULT_REACHABILITY_LOCK_DIR,
) -> dict[str, Any]:
    """Run reachability for a single sample result if it has submitted PoCs."""
    started = time.monotonic()
    metadata: dict[str, Any] = {
        "enabled": enabled,
        "model": model_namespace,
        "sample_id": sample_id,
        "timeout": timeout,
        "debugger_image": debugger_image,
        "max_hits_per_event": max_hits_per_event,
        "concurrency": max(1, int(concurrency)),
    }
    if not enabled:
        metadata.update({"status": "skipped", "reason": "disabled"})
        _write_pipeline_metadata(sample_result_dir, metadata)
        update_manifest_reachability(sample_result_dir, metadata)
        return metadata

    has_input, reason = sample_has_reachability_input(sample_result_dir)
    if not has_input:
        metadata.update({"status": "skipped", "reason": reason})
        _write_pipeline_metadata(sample_result_dir, metadata)
        update_manifest_reachability(sample_result_dir, metadata)
        return metadata

    try:
        with reachability_slot(lock_dir, concurrency) as slot:
            metadata["slot"] = slot
            result = evaluate_model_sample(
                model=model_namespace,
                sample_id=sample_id,
                sample_dir=sample_result_dir,
                timeout=timeout,
                debugger_image=debugger_image,
                max_hits_per_event=max_hits_per_event,
            )
        metadata["seconds"] = round(time.monotonic() - started, 1)
        if "skipped" in result:
            metadata.update({"status": "skipped", "reason": result["skipped"]})
        elif result.get("runtime_status") == "runtime_spec_unavailable":
            metadata.update(
                {
                    "status": "skipped",
                    "reason": "unavailable_runtime_spec",
                    "error": result.get("error"),
                }
            )
        elif "error" in result:
            metadata.update({"status": "failed", "error": result["error"]})
        else:
            summary = result.get("summary") or {}
            metadata.update(
                {
                    "status": "complete",
                    "summary": summary,
                    "report_path": "reachability_eval.json",
                }
            )
    except Exception as exc:  # noqa: BLE001 - do not mask PoC generation.
        metadata.update(
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=8),
                "seconds": round(time.monotonic() - started, 1),
            }
        )
    _write_pipeline_metadata(sample_result_dir, metadata)
    update_manifest_reachability(sample_result_dir, metadata)
    return metadata


def _write_pipeline_metadata(sample_result_dir: Path, metadata: dict[str, Any]) -> None:
    sample_result_dir.mkdir(parents=True, exist_ok=True)
    (sample_result_dir / "reachability_pipeline.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
