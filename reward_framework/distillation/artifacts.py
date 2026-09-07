"""Small shared readers and writers for distillation run artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .defaults import REPO_ROOT

GT_RESULTS = REPO_ROOT / "gt_results"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_if_exists(path: Path) -> Any:
    """Read a JSON file, or return None when it is absent or unreadable."""
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sample_project(sample_id: str) -> str | None:
    """Project id for pool bookkeeping; kept out of every model-facing payload."""
    info = load_json_if_exists(GT_RESULTS / sample_id / "sample_info.json") or {}
    project = info.get("project")
    if isinstance(project, dict):
        return project.get("id")
    return str(project) if project else None
