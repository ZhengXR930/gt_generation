import json
import sys
from types import SimpleNamespace

import pytest

from gt_generation import gt_plugin


def _write_portability_ok(result_dir):
    (result_dir / "portability_report.json").write_text(json.dumps({
        "runtime_portable": True,
        "clean_replay_ok": True,
    }))


def test_stage_existing_runtime_candidate_copies_only_lightweight_hints(tmp_path):
    published = tmp_path / "published"
    migration = tmp_path / "migration"
    published.mkdir()
    migration.mkdir()
    (published / "runtime_support").mkdir()
    (published / "runtime_support" / "harness.c").write_text("int main() {}\n")
    (published / "_work").mkdir()
    (published / "_work" / "binary").write_bytes(b"binary")
    (published / "runtime_work.tar.gz").write_bytes(b"archive")
    (published / "runtime_spec.json").write_text(json.dumps({
        "build_commands": ["cc /gt/runtime_support/harness.c -o /gt/_work/repro"]
    }))
    (published / "runtime_build.json").write_text(json.dumps({
        "commands": [{
            "command": (
                "cc /gt/runtime_support/harness.c "
                "> /gt/build_vulnerable.log 2>&1"
            )
        }]
    }))
    (published / "reproduction_report.json").write_text("{}")
    (published / "build_vulnerable.log").write_text("compiler output\n")

    result = gt_plugin.stage_existing_runtime_candidate(published, migration)

    assert result["staged"] is True
    assert (migration / "stage01_candidate_runtime_spec.json").is_file()
    assert (migration / "stage01_candidate_runtime_build.json").is_file()
    assert (migration / "stage01_candidate_reproduction_report.json").is_file()
    assert (migration / "runtime_support" / "harness.c").is_file()
    assert not (migration / "build_vulnerable.log").exists()
    assert not (migration / "_work").exists()
    assert not (migration / "runtime_work.tar.gz").exists()


def test_stage01_migration_copy_excludes_generated_runtime_roots(tmp_path):
    published = tmp_path / "published"
    staging = tmp_path / "staging"
    published.mkdir()
    (published / "ground_truth.json").write_text("{}")
    (published / "_work" / "src").mkdir(parents=True)
    (published / "_work" / "src" / "object.o").write_bytes(b"object")
    (published / "_out").mkdir()
    (published / "_out" / "target").write_bytes(b"binary")
    (published / "runtime_build_logs").mkdir()
    (published / "runtime_build_logs" / "build.log").write_text("log")
    (published / "runtime_work.tar.gz").write_bytes(b"archive")
    (published / "runtime_work.tar.gz.part-000").write_bytes(b"part")
    (published / "runtime_work_manifest.json").write_text("{}")

    gt_plugin._copy_published_package_for_migration(
        published, staging, ("runtime_work.tar.gz",)
    )

    assert (staging / "ground_truth.json").is_file()
    assert not (staging / "_work").exists()
    assert not (staging / "_out").exists()
    assert not (staging / "runtime_build_logs").exists()
    assert not (staging / "runtime_work.tar.gz").exists()
    assert not (staging / "runtime_work.tar.gz.part-000").exists()
    assert not (staging / "runtime_work_manifest.json").exists()


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


def test_stage01_migration_skips_only_portability_complete_samples(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gt_plugin, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "gt_generation.gt_toolkit.portability.portability_gate_passes",
        lambda path: path.name == "portable",
    )

    skipped = gt_plugin.completed_samples_to_skip(
        ["legacy_complete", "portable"],
        {"start_at": "", "run_mode": "stage01_screening"},
    )

    assert skipped == ["portable"]


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


def test_stage01_migration_requires_stage01_only_mode(tmp_path):
    config = {
        "cli": "codex",
        "model": "gpt-5.4-2026-03-05",
        "parallel_dockers": 1,
        "repo_docker_context": str(
            gt_plugin.REPO_ROOT / "docker" / "gt-memory-env"
        ),
        "samples": ["nvd_CVE-2026-2241"],
        "stage01_migration": True,
    }
    path = tmp_path / "invalid-migration.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(SystemExit, match="requires stop_after='01_reproducer'"):
        gt_plugin.load_config(path)


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
    _write_portability_ok(tmp_path)

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


def test_stage01_screening_marks_clone_failure_as_infra_retryable(tmp_path):
    (tmp_path / "prepare_report.json").write_text(
        json.dumps(
            {
                "track": "repo/osv",
                "prepared": False,
                "reason": "clone failed: https://github.com/net-snmp/net-snmp",
                "clone_errors": [
                    {
                        "reason": "required commit fetch failed",
                        "stderr": "fatal: unable to access repo: Could not resolve host: github.com",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    screening = gt_plugin.evaluate_stage01_screening(
        tmp_path, {"sample_id": "osv_ossfuzz_OSV-2025-133"}, 1
    )

    assert screening["status"] == "infrastructure_retryable"
    assert screening["accepted_for_gt"] is False
    assert screening["failure_class"] == "infrastructure"
    assert screening["retryable"] is True
    assert screening["reason"] == "source_materialization_infrastructure_failure"


def test_stage01_screening_marks_dependency_build_failure_as_infra_retryable(tmp_path):
    (tmp_path / "prepare_report.json").write_text(
        json.dumps({"track": "repo/osv", "prepared": True}), encoding="utf-8"
    )
    (tmp_path / "sample_info.json").write_text(
        json.dumps({"fix_commit": "fixed"}), encoding="utf-8"
    )
    (tmp_path / "reproduction_report.json").write_text(
        json.dumps(
            {
                "vulnerable_reproduced": False,
                "matches_issue": False,
                "crash_summary": (
                    "authoritative OSS-Fuzz build did not complete because "
                    "/install/ruzzy is absent in gt-memory-env"
                ),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "portability_report.json").write_text(
        json.dumps(
            {
                "runtime_portable": False,
                "clean_replay_ok": False,
                "vulnerable_build_ok": False,
                "reason": "vulnerable reproduction was not established",
            }
        ),
        encoding="utf-8",
    )

    screening = gt_plugin.evaluate_stage01_screening(
        tmp_path, {"sample_id": "osv_ossfuzz_OSV-2025-1001"}, 1
    )

    assert screening["status"] == "infrastructure_retryable"
    assert screening["accepted_for_gt"] is False
    assert screening["failure_class"] == "infrastructure"
    assert screening["reason"] == "vulnerable_build_infrastructure_failure"


def test_stage01_screening_marks_fixed_build_failure_as_infra_retryable(tmp_path):
    (tmp_path / "prepare_report.json").write_text(
        json.dumps({"track": "repo/osv", "prepared": True}), encoding="utf-8"
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
                "fixed_oracle": {
                    "checked": True,
                    "acceptable": False,
                    "summary": "fixed build failed: Failed to connect to github.com",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "portability_report.json").write_text(
        json.dumps(
            {
                "runtime_portable": False,
                "clean_replay_ok": False,
                "vulnerable_build_ok": True,
                "fixed_build_ok": False,
            }
        ),
        encoding="utf-8",
    )

    screening = gt_plugin.evaluate_stage01_screening(
        tmp_path, {"sample_id": "osv_ossfuzz_OSV-2025-133"}, 1
    )

    assert screening["status"] == "infrastructure_retryable"
    assert screening["accepted_for_gt"] is False
    assert screening["failure_class"] == "infrastructure"
    assert screening["reason"] == "fixed_build_infrastructure_failure"
