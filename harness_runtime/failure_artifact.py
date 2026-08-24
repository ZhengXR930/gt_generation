"""Utilities for durable per-sample failure artifacts.

These helpers are intentionally small: they only write a diagnostic manifest
when a harness fails before its normal result persistence path can run.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _copy_file(src: Path, dst: Path) -> bool:
    try:
        if not src.is_file():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() == dst.resolve():
            return True
        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def write_failure_artifact(
    sample_dir: Path,
    *,
    sample_id: str,
    harness: str,
    model: Optional[str] = None,
    framework: str = "",
    evaluation_protocol: str = "poc_analysis_artifact_per_submission_v3_failure",
    status: str = "error",
    stop_reason: str = "runner_failure",
    error: Optional[str] = None,
    returncode: Optional[int] = None,
    timed_out: Optional[bool] = None,
    seconds: Optional[float] = None,
    command: Any = None,
    log_path: Optional[Path] = None,
    checkpoint_files: Optional[Mapping[str, Path]] = None,
    extra: Optional[Mapping[str, Any]] = None,
    overwrite_manifest: bool = False,
) -> dict[str, Any]:
    """Write a minimal manifest/checkpoint for a failed sample.

    Normal runner-specific manifests are more detailed and should win.  Callers
    therefore leave ``overwrite_manifest`` false unless they know this helper is
    the canonical terminal path for the sample.
    """
    sample_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = sample_dir / "checkpoint"
    checkpoint.mkdir(parents=True, exist_ok=True)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    copied_files: dict[str, bool] = {}
    if command is not None:
        (checkpoint / "command.json").write_text(
            json.dumps(_json_safe(command), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        copied_files["command.json"] = True
    if log_path is not None:
        copied_files["agent.log"] = _copy_file(log_path, checkpoint / "agent.log")
    for name, src in (checkpoint_files or {}).items():
        copied_files[name] = _copy_file(src, checkpoint / name)

    failure = {
        "created_at": created_at,
        "sample_id": sample_id,
        "framework": framework,
        "harness": harness,
        "model": model,
        "status": status,
        "stop_reason": stop_reason,
        "returncode": returncode,
        "timed_out": timed_out,
        "seconds": seconds,
        "error": error,
    }
    if extra:
        failure["extra"] = _json_safe(dict(extra))
    (checkpoint / "failure.json").write_text(
        json.dumps(failure, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (checkpoint / "status.json").write_text(
        json.dumps(failure, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "evaluation_protocol": evaluation_protocol,
        "sample_id": sample_id,
        "framework": framework,
        "harness": harness,
        "model": model,
        "status": status,
        "returncode": returncode,
        "timed_out": timed_out,
        "stop_reason": stop_reason,
        "seconds": seconds,
        "error": error,
        "poc_generation": {
            "ok": False,
            "success": False,
            "submission_attempts": [],
        },
        "num_submission_attempts": 0,
        "submission_attempts": [],
        "poc_deduplication": {
            "input_attempts": 0,
            "unique_pocs": 0,
        },
        "deduplicated_pocs": [],
        "analysis": {
            "produced": False,
            "source": "none",
            "path": "analysis.json",
            "format": "JSON object with sample_id, fine_trace, and vuln_logic",
        },
        "checkpoint": {
            "dir": "checkpoint/",
            "phase": stop_reason,
            "contains_failure_json": True,
            "copied_files": copied_files,
        },
        "created_at": created_at,
    }
    if extra:
        extra_payload = _json_safe(dict(extra))
        collided_extra = {}
        for key, value in extra_payload.items():
            if key in manifest:
                collided_extra[key] = value
            else:
                manifest[key] = value
        if collided_extra:
            manifest["extra"] = collided_extra

    manifest_path = sample_dir / "manifest.json"
    if overwrite_manifest or not manifest_path.is_file():
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return manifest
