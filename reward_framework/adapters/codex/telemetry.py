"""Codex adapter telemetry helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def summarize_result(result_dir: Path) -> dict[str, Any]:
    manifest_path = result_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    return {
        "adapter": "codex",
        "result_dir": str(result_dir),
        "status": manifest.get("status"),
        "sample_id": manifest.get("sample_id"),
        "submission_attempts": manifest.get("submission_attempts", []),
        "analysis_exists": (result_dir / "analysis.json").is_file(),
    }
