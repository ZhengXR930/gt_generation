import json
import sys
from types import SimpleNamespace

import pytest

from gt_generation import gt_plugin


def test_partial_repair_does_not_skip_complete_samples(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "gt_status",
        SimpleNamespace(classify=lambda sample_id: ("complete", "")),
    )

    assert gt_plugin.completed_samples_to_skip(
        ["arvo_1"], {"start_at": "02_fine_trace"}
    ) == []
    assert gt_plugin.completed_samples_to_skip(
        ["arvo_1"], {"start_at": ""}
    ) == ["arvo_1"]


def test_config_loads_partial_repair_bounds(tmp_path):
    config = {
        "cli": "codex",
        "model": "gpt-5.4-2026-03-05",
        "parallel_dockers": 1,
        "repo_docker_context": str(
            gt_plugin.REPO_ROOT / "docker" / "gt-memory-env"
        ),
        "samples": ["arvo_1"],
        "start_at": "02_fine_trace",
        "stop_after": "05_validate",
    }
    path = tmp_path / "repair.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    loaded = gt_plugin.load_config(path)

    assert loaded["start_at"] == "02_fine_trace"
    assert loaded["stop_after"] == "05_validate"


def test_config_forces_partial_repair_through_validation(tmp_path):
    config = {
        "cli": "codex",
        "model": "gpt-5.4-2026-03-05",
        "parallel_dockers": 1,
        "repo_docker_context": str(
            gt_plugin.REPO_ROOT / "docker" / "gt-memory-env"
        ),
        "samples": ["arvo_1"],
        "start_at": "03_trace_review",
    }
    path = tmp_path / "repair.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    assert gt_plugin.load_config(path)["stop_after"] == "05_validate"


def test_config_rejects_partial_repair_that_stops_before_validation(tmp_path):
    config = {
        "cli": "codex",
        "model": "gpt-5.4-2026-03-05",
        "parallel_dockers": 1,
        "repo_docker_context": str(
            gt_plugin.REPO_ROOT / "docker" / "gt-memory-env"
        ),
        "samples": ["arvo_1"],
        "start_at": "02_fine_trace",
        "stop_after": "04_assertion_validator",
    }
    path = tmp_path / "repair.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(SystemExit, match="must run through 05_validate"):
        gt_plugin.load_config(path)
