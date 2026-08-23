"""Read OpenHands adapter telemetry from completed PoC result directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def summarize_result(result_dir: Path) -> dict[str, Any]:
    """Return a compact, adapter-stable summary for Teacher observations."""
    manifest = _read_json(result_dir / "manifest.json") or {}
    reachability = _read_json(result_dir / "reachability_eval.json") or {}
    summary = reachability.get("summary") if isinstance(reachability, dict) else None
    attempts = manifest.get("submission_attempts") or manifest.get("poc_generation", {}).get(
        "submission_attempts", []
    )
    return {
        "result_dir": str(result_dir),
        "status": manifest.get("status"),
        "sample_id": manifest.get("sample_id") or manifest.get("task_id"),
        "model": manifest.get("model"),
        "agent_action_count": manifest.get("agent_action_count"),
        "num_submission_attempts": len(attempts) if isinstance(attempts, list) else 0,
        "submission_attempts": attempts if isinstance(attempts, list) else [],
        "reachability_summary": summary,
        "analysis_exists": (result_dir / "analysis.json").is_file(),
        "manifest_exists": (result_dir / "manifest.json").is_file(),
        "reachability_exists": (result_dir / "reachability_eval.json").is_file(),
    }
