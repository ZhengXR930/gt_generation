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


@pytest.mark.parametrize(
    "start_at",
    [
        "04_assertion_plan",
        "04_instrument_vulnerable",
        "04_instrument_fixed",
        "04_assertion_execute",
        "04_reachability",
    ],
)
def test_config_forces_split_assertion_repairs_through_validation(
    tmp_path, start_at
):
    config = {
        "cli": "codex",
        "model": "gpt-5.4-2026-03-05",
        "parallel_dockers": 1,
        "repo_docker_context": str(
            gt_plugin.REPO_ROOT / "docker" / "gt-memory-env"
        ),
        "samples": ["arvo_1"],
        "start_at": start_at,
    }
    path = tmp_path / "repair.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    loaded = gt_plugin.load_config(path)

    assert loaded["start_at"] == start_at
    assert loaded["stop_after"] == "05_validate"


def test_config_maps_legacy_assertion_stage_to_plan_and_validates(tmp_path):
    config = {
        "cli": "codex",
        "model": "gpt-5.4-2026-03-05",
        "parallel_dockers": 1,
        "repo_docker_context": str(
            gt_plugin.REPO_ROOT / "docker" / "gt-memory-env"
        ),
        "samples": ["arvo_1"],
        "start_at": "04_assertion_validator",
    }
    path = tmp_path / "repair.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    loaded = gt_plugin.load_config(path)

    assert loaded["start_at"] == "04_assertion_plan"
    assert loaded["stop_after"] == "05_validate"


def test_config_loads_explicit_repair_staging_reuse(tmp_path):
    config = {
        "cli": "codex",
        "model": "gpt-5.4-2026-03-05",
        "parallel_dockers": 1,
        "repo_docker_context": str(
            gt_plugin.REPO_ROOT / "docker" / "gt-memory-env"
        ),
        "samples": ["arvo_1"],
        "start_at": "04_assertion_plan",
        "reuse_repair_staging": True,
    }
    path = tmp_path / "repair.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    loaded = gt_plugin.load_config(path)

    assert loaded["reuse_repair_staging"] is True
    assert loaded["stop_after"] == "05_validate"


@pytest.mark.parametrize(
    "start_at",
    [
        "02_fine_trace",
        "04_assertion_plan",
        "04_instrument_vulnerable",
        "04_instrument_fixed",
        "04_assertion_execute",
        "04_reachability",
    ],
)
def test_config_rejects_partial_repair_that_stops_before_validation(
    tmp_path, start_at
):
    config = {
        "cli": "codex",
        "model": "gpt-5.4-2026-03-05",
        "parallel_dockers": 1,
        "repo_docker_context": str(
            gt_plugin.REPO_ROOT / "docker" / "gt-memory-env"
        ),
        "samples": ["arvo_1"],
        "start_at": start_at,
        "stop_after": "04_reachability",
    }
    path = tmp_path / "repair.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(SystemExit, match="must run through 05_validate"):
        gt_plugin.load_config(path)


def test_config_marks_stage01_only_run_as_screening(tmp_path):
    config = {
        "cli": "codex",
        "model": "gpt-5.4-2026-03-05",
        "parallel_dockers": 1,
        "repo_docker_context": str(
            gt_plugin.REPO_ROOT / "docker" / "gt-memory-env"
        ),
        "samples": ["nvd_CVE-2026-2241"],
        "stop_after": "01_reproducer",
    }
    path = tmp_path / "screen.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    loaded = gt_plugin.load_config(path)

    assert loaded["run_mode"] == "stage01_screening"


def test_stage01_screening_accepts_without_package_audit(tmp_path):
    (tmp_path / "prepare_report.json").write_text(
        json.dumps({"track": "repo/nvd"}), encoding="utf-8"
    )
    (tmp_path / "sample_info.json").write_text(
        json.dumps({"fix_commit": "fixed"}), encoding="utf-8"
    )
    (tmp_path / "reproduction_report.json").write_text(
        json.dumps(
            {
                "vulnerable_reproduced": True,
                "matches_issue": True,
                "fixed_oracle_checked": True,
                "fixed_oracle_acceptable": True,
                "setup_command": "/gt/build.sh 'set -euo pipefail\nninja'",
            }
        ),
        encoding="utf-8",
    )

    screening = gt_plugin.evaluate_stage01_screening(
        tmp_path, {"sample_id": "nvd_CVE-2026-2241"}, 0
    )

    assert screening["status"] == "accepted_for_gt"
    assert screening["accepted_for_gt"] is True
    assert not (tmp_path / "stage01_screening.json").exists()


def test_stage01_screening_rejects_unclean_fixed_oracle(tmp_path):
    (tmp_path / "prepare_report.json").write_text(
        json.dumps({"track": "repo/osv"}), encoding="utf-8"
    )
    (tmp_path / "sample_info.json").write_text(
        json.dumps({"fix_commit": "fixed"}), encoding="utf-8"
    )
    (tmp_path / "reproduction_report.json").write_text(
        json.dumps(
            {
                "vulnerable_reproduced": True,
                "matches_issue": True,
                "fixed_oracle_checked": True,
                "fixed_oracle_acceptable": False,
                "setup_command": "/gt/build.sh 'set -euo pipefail\nninja'",
            }
        ),
        encoding="utf-8",
    )

    screening = gt_plugin.evaluate_stage01_screening(
        tmp_path, {"sample_id": "osv_1"}, 1
    )

    assert screening["status"] == "rejected_by_stage01"
    assert screening["accepted_for_gt"] is False
    assert screening["reason"] == "fixed_oracle_not_clean"
