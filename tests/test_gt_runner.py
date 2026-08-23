import json
import os
from pathlib import Path

import pytest

from gt_generation import runner


def _result(name: str, ok: bool) -> runner.StageResult:
    return runner.StageResult(
        name=name,
        command=name,
        returncode=0 if ok else 1,
        started_at="start",
        ended_at="end",
        stdout_path="",
        stderr_path="",
        required_outputs_ok=ok,
        success_check_ok=ok,
    )


def test_repo_fixed_oracle_gate_rejects_masked_setup_command(tmp_path):
    runner.write_json(tmp_path / "prepare_report.json", {"track": "repo/secbench"})
    runner.write_json(
        tmp_path / "sample_info.json",
        {
            "sample_id": "sample",
            "fix_commit": "fixed",
        },
    )
    runner.write_json(
        tmp_path / "reproduction_report.json",
        {
            "fixed_oracle_checked": True,
            "fixed_oracle_acceptable": True,
            "setup_command": "/tmp/result/build.sh 'set -euo pipefail\nninja || true'",
        },
    )

    assert runner.repo_fixed_oracle_gate_passes(
        {"result_dir": str(tmp_path)},
        "{result_dir}/reproduction_report.json",
    ) is False


def test_workflow_requires_stage01_portability_gate():
    workflow = json.loads(
        (Path(__file__).parents[1] / "gt_generation" / "workflow.json").read_text()
    )
    stage = next(item for item in workflow["stages"] if item["name"] == "01_reproducer")

    assert "{result_dir}/portability_report.json" not in stage["required_outputs"]
    assert stage["success_check"]["portability_gate"] is True


def test_repo_gate_rejects_missing_portability_report(tmp_path):
    runner.write_json(tmp_path / "prepare_report.json", {"track": "repo/secbench"})
    runner.write_json(
        tmp_path / "sample_info.json",
        {"sample_id": "sample", "fix_commit": "fixed"},
    )
    runner.write_json(
        tmp_path / "reproduction_report.json",
        {
            "fixed_oracle_checked": True,
            "fixed_oracle_acceptable": True,
            "setup_command": "make",
        },
    )
    stage = {
        "success_check": {
            "path": "{result_dir}/reproduction_report.json",
            "repo_fixed_oracle_gate": True,
            "portability_gate": True,
            "all": [{"field": "fixed_oracle_checked", "equals": True}],
        }
    }

    assert runner.check_success(stage, {"result_dir": str(tmp_path)}) is False


def test_stage01_only_filter_does_not_select_final_validation():
    workflow = json.loads(
        (Path(__file__).parents[1] / "gt_generation" / "workflow.json").read_text()
    )
    stages = workflow["stages"]

    should_run = runner.make_stage_filter(
        stages, start_at="", stop_after="01_reproducer", only=""
    )

    assert should_run("00_prepare") is True
    assert should_run("01_reproducer") is True
    assert should_run("05_validate") is False


def test_workflow_splits_assertion_plan_execution_and_reachability():
    workflow = json.loads(
        (Path(__file__).parents[1] / "gt_generation" / "workflow.json").read_text()
    )
    assert workflow["compact_on_success"] is True
    agent_stages = [stage for stage in workflow["stages"] if stage.get("role")]

    assert [stage["name"] for stage in agent_stages] == [
        "01_reproducer",
        "02_fine_trace",
        "03_trace_review",
        "04_assertion_plan",
        "04_instrument_vulnerable",
        "04_instrument_fixed",
        "04_assertion_execute",
    ]
    assert len({stage["role"] for stage in agent_stages}) == 7
    review = next(stage for stage in agent_stages if stage["name"] == "03_trace_review")
    assert review["feedback_to"] == "02_fine_trace"
    assert review["runtime_role"] == "roles/02_runtime_disambiguation.md"
    assert review["incremental_role"] == "roles/03_trace_review_incremental.md"
    assert review["feedback_rounds"] == 2
    plan_stage = next(
        stage for stage in agent_stages if stage["name"] == "04_assertion_plan"
    )
    assert plan_stage["success_check"] == {
        "path": "{result_dir}/assertion_preflight.json",
        "field": "ok",
        "equals": True,
    }
    assertion_stage = next(
        stage for stage in agent_stages if stage["name"] == "04_assertion_execute"
    )
    assert assertion_stage["success_check"] == {
        "path": "{result_dir}/assertion_results.json",
        "field": "required_verified",
        "equals": True,
    }
    reachability_stage = next(
        stage for stage in workflow["stages"] if stage["name"] == "04_reachability"
    )
    assert not reachability_stage.get("role")
    assert "gt_toolkit reachability --for-result-dir" in (
        reachability_stage["command_template"]
    )
    assert reachability_stage["success_check"] == {
        "path": "{result_dir}/reachability_report.json",
        "reachability_gate": True,
    }
    prepare_stage = next(
        stage for stage in workflow["stages"] if stage["name"] == "00_prepare"
    )
    assert {
        "{result_dir}/sample_info.json",
        "{result_dir}/default_crash_trace.txt",
        "{result_dir}/build.sh",
        "{result_dir}/poc",
        "{result_dir}/patch.diff",
    }.issubset(set(prepare_stage["required_outputs"]))
    final_stage = next(
        stage for stage in workflow["stages"] if stage["name"] == "05_validate"
    )
    assert "gt_toolkit audit-package" in final_stage["command_template"]
    assert "gt_toolkit validate {gt_path} --strict --json" in final_stage["command_template"]
    assert "gt_toolkit bind-evidence" in final_stage["command_template"]
    assert "gt_toolkit reachability" not in final_stage["command_template"]
    assert next(
        stage for stage in workflow["stages"] if stage["name"] == "02_fine_trace"
    )["validate_strict"] is True


def test_claude_adapter_uses_one_model_no_stage_escalation():
    # The config-driven plugin runs every stage with a single model (no
    # per-stage escalation); the adapter takes it from GT_AGENT_MODEL and must
    # not branch on the stage/role name.
    adapter = (
        Path(__file__).parents[1]
        / "gt_generation"
        / "adapters"
        / "claude_code"
        / "gt_agent_claude.sh"
    ).read_text()

    assert 'GT_AGENT_MODEL' in adapter
    assert '02_*|03_*|04_*' not in adapter
    assert 'GT_CLAUDE_COMPLEX_MODEL' not in adapter


def test_codex_adapter_uses_model_reasoning_and_strict_config():
    adapter = (
        Path(__file__).parents[1]
        / "gt_generation"
        / "adapters"
        / "codex"
        / "gt_agent_codex.sh"
    ).read_text()

    assert 'GT_AGENT_MODEL' in adapter
    assert 'GT_AGENT_REASONING_EFFORT' in adapter
    assert 'model_reasoning_effort=' in adapter
    assert '--strict-config' in adapter


def test_codex_adapter_waits_for_codex_so_bridge_cleanup_runs():
    adapter = (
        Path(__file__).parents[1]
        / "gt_generation"
        / "adapters"
        / "codex"
        / "gt_agent_codex.sh"
    ).read_text()

    assert "trap cleanup_bridge EXIT" in adapter
    assert 'exec codex "${CODEX_ARGS[@]}"' not in adapter
    assert 'codex "${CODEX_ARGS[@]}"' in adapter
    assert 'exit "$CODEX_STATUS"' in adapter


def test_changed_json_paths_include_added_list_items():
    changed = []

    runner.collect_changed_json_paths(
        {"trace": [{"step": 1}]},
        {"trace": [{"step": 1}, {"step": 2}, {"step": 3}]},
        "$",
        changed,
    )

    assert changed == ["$.trace.length", "$.trace[1]", "$.trace[2]"]


def test_runtime_disambiguation_can_be_enabled_per_invocation():
    assert runner.runtime_disambiguation_enabled({}) is False
    assert runner.runtime_disambiguation_enabled(
        {"runtime_disambiguation": False}, command_line_enabled=True
    ) is True
    assert runner.runtime_disambiguation_enabled(
        {"runtime_disambiguation": True}
    ) is True


def test_runtime_feedback_requires_existing_flag_and_observe(tmp_path):
    assert runner.feedback_requests_runtime_disambiguation(tmp_path) is False
    runner.write_json(
        tmp_path / "trace_feedback.json",
        {"needs_runtime_disambiguation": True, "observe": ""},
    )
    assert runner.feedback_requests_runtime_disambiguation(tmp_path) is False
    runner.write_json(
        tmp_path / "trace_feedback.json",
        {
            "needs_runtime_disambiguation": True,
            "observe": "correlate producer and consumer offsets",
        },
    )
    assert runner.feedback_requests_runtime_disambiguation(tmp_path) is True


def test_malformed_runtime_feedback_falls_back_to_static_repair(tmp_path):
    (tmp_path / "trace_feedback.json").write_text(
        '{"needs_runtime_disambiguation": true, "issues": [}',
        encoding="utf-8",
    )

    assert runner.feedback_requests_runtime_disambiguation(tmp_path) is False


def test_runtime_stage_requires_fresh_completed_workspace_cycle(tmp_path):
    variables = {"result_dir": str(tmp_path)}
    stage = {"name": "02_runtime_disambiguation"}
    workspace = tmp_path / "arvo_workspace"
    workspace.mkdir()
    runner.write_json(
        tmp_path / "arvo_workspace.json",
        {
            "phase": "vulnerable_source_reset",
            "vulnerable_compile_returncode": 0,
            "vulnerable_expectation_matched": True,
        },
    )
    for name in (
        "instrumentation_apply.log",
        "vulnerable_incremental_compile.log",
        "vulnerable_run.log",
        "reset_source.log",
    ):
        (workspace / name).write_text("ok")

    assert runner.check_runtime_disambiguation_success(stage, variables, 0) is True
    (workspace / "vulnerable_run.log").unlink()
    assert runner.check_runtime_disambiguation_success(stage, variables, 0) is False
    assert runner.check_runtime_disambiguation_success(
        {"name": "02_fine_trace"}, variables, 0
    ) is True


def test_assertion_preflight_must_precede_runtime_traces(tmp_path):
    from gt_generation.gt_toolkit.evidence import file_sha256

    stage = {"name": "04_assertion_execute"}
    variables = {"result_dir": str(tmp_path)}
    runner.write_json(
        tmp_path / "candidate_assertions.json",
        {"content_hash": "sha256:plan"},
    )
    for name in (
        "field_bindings.json",
        "event_locations.json",
    ):
        runner.write_json(tmp_path / name, {})
    for version in ("vulnerable", "fixed"):
        patch = tmp_path / f"{version}-instrumentation.patch"
        patch.write_text("patch", encoding="utf-8")
        runner.write_json(
            tmp_path / f"{version}_instrumentation_preflight.json",
            {
                "ok": True,
                "version": version,
                "assertion_content_hash": "sha256:plan",
                "check": {
                    "patch_sha256": file_sha256(patch),
                    "apply_returncode": 0,
                    "compile_returncode": 0,
                },
            },
        )
    root_node = {
        "invariant_id": "root",
        "role": "root_cause",
        "file": "src/a.c",
        "function": "parse",
        "line": 10,
        "operands": ["length", "capacity"],
        "relation": {"op": "le", "left": "length", "right": "capacity"},
        "verified": True,
    }
    candidate_graph = {
        "root_cause_criterion": {"invariant_id": "root"},
        "nodes": [root_node],
        "edges": [],
    }
    runner.write_json(tmp_path / "candidate_invariants.json", candidate_graph)
    runner.write_json(tmp_path / "verified_invariants.json", candidate_graph)
    spec_path = tmp_path / "candidate_assertions.json"
    runner.write_json(
        tmp_path / ".assertion_spec_frozen.json",
        {
            "content_hash": "sha256:plan",
            "file_sha256": file_sha256(spec_path),
        },
    )
    runner.write_json(
        tmp_path / "assertion_preflight.json",
        {
            "ok": True,
            "assertion_content_hash": "sha256:plan",
            "input_hashes": {
                name: file_sha256(tmp_path / name)
                for name in (
                    "candidate_assertions.json",
                    "candidate_invariants.json",
                    "field_bindings.json",
                    "event_locations.json",
                )
            },
        },
    )
    for name in ("vulnerable_assertion_trace.txt", "fixed_assertion_trace.txt"):
        (tmp_path / name).write_text("trace")

    assert runner.check_assertion_preflight_success(stage, variables) is True
    newer = (tmp_path / "assertion_preflight.json").stat().st_mtime + 10
    os.utime(tmp_path / "assertion_preflight.json", (newer, newer))
    assert runner.check_assertion_preflight_success(stage, variables) is False


def test_assertion_plan_gate_does_not_require_runtime_outputs(tmp_path):
    variables = {"result_dir": str(tmp_path)}
    runner.write_json(
        tmp_path / "candidate_assertions.json",
        {"content_hash": "sha256:plan"},
    )
    for name in (
        "candidate_invariants.json",
        "field_bindings.json",
        "event_locations.json",
    ):
        runner.write_json(tmp_path / name, {})
    from gt_generation.gt_toolkit.evidence import file_sha256

    runner.write_json(
        tmp_path / ".assertion_spec_frozen.json",
        {
            "content_hash": "sha256:plan",
            "file_sha256": file_sha256(tmp_path / "candidate_assertions.json"),
        },
    )
    inputs = (
        "candidate_assertions.json",
        "candidate_invariants.json",
        "field_bindings.json",
        "event_locations.json",
    )
    runner.write_json(
        tmp_path / "assertion_preflight.json",
        {
            "ok": True,
            "assertion_content_hash": "sha256:plan",
            "input_hashes": {
                name: file_sha256(tmp_path / name)
                for name in inputs
            },
        },
    )

    assert runner.check_assertion_stage_success(
        {"name": "04_assertion_plan"}, variables
    ) is True


def test_arvo_instrumentation_stage_requires_only_its_side_build(tmp_path):
    variables = {"result_dir": str(tmp_path)}
    runner.write_json(
        tmp_path / "candidate_assertions.json",
        {"sample_id": "arvo_42", "content_hash": "sha256:plan"},
    )
    for name in (
        "candidate_invariants.json",
        "field_bindings.json",
        "event_locations.json",
    ):
        runner.write_json(tmp_path / name, {})
    from gt_generation.gt_toolkit.evidence import file_sha256

    runner.write_json(
        tmp_path / ".assertion_spec_frozen.json",
        {
            "content_hash": "sha256:plan",
            "file_sha256": file_sha256(tmp_path / "candidate_assertions.json"),
        },
    )
    inputs = (
        "candidate_assertions.json",
        "candidate_invariants.json",
        "field_bindings.json",
        "event_locations.json",
    )
    input_hashes = {
        name: file_sha256(tmp_path / name)
        for name in inputs
    }
    runner.write_json(
        tmp_path / "assertion_preflight.json",
        {
            "ok": True,
            "assertion_content_hash": "sha256:plan",
            "input_hashes": input_hashes,
        },
    )
    patch = tmp_path / "vulnerable-instrumentation.patch"
    patch.write_text("patch", encoding="utf-8")
    runner.write_json(
        tmp_path / "vulnerable_instrumentation_preflight.json",
        {
            "ok": True,
            "version": "vulnerable",
            "assertion_content_hash": "sha256:plan",
            "check": {
                "patch_sha256": file_sha256(patch),
                "apply_returncode": 0,
                "compile_returncode": 0,
            },
        },
    )
    stage = {"name": "04_instrument_vulnerable"}
    assert runner.check_assertion_stage_success(stage, variables) is True

    report = runner.load_json(
        tmp_path / "vulnerable_instrumentation_preflight.json"
    )
    report["check"]["compile_returncode"] = 2
    runner.write_json(
        tmp_path / "vulnerable_instrumentation_preflight.json", report
    )
    assert runner.check_assertion_stage_success(stage, variables) is False


def test_split_stage_entry_and_retry_remove_only_owned_outputs(tmp_path):
    plan_files = {
        "candidate_assertions.json",
        "candidate_invariants.json",
        "field_bindings.json",
        "event_locations.json",
        ".assertion_spec_frozen.json",
        "assertion_preflight.json",
    }
    vulnerable_files = {
        "vulnerable-instrumentation.patch",
        "vulnerable_instrumentation_preflight.json",
    }
    fixed_files = {
        "fixed-instrumentation.patch",
        "fixed_instrumentation_preflight.json",
    }
    execution_files = {
        "vulnerable_assertion_trace.txt",
        "fixed_assertion_trace.txt",
        "assertion_results.json",
        "perturbation_results.json",
        "verified_assertions.json",
        "verified_invariants.json",
    }
    for name in (
        plan_files
        | vulnerable_files
        | fixed_files
        | execution_files
        | {"reachability_report.json"}
    ):
        (tmp_path / name).write_text("old", encoding="utf-8")

    runner.prepare_stage_entry("04_instrument_fixed", tmp_path)
    assert all((tmp_path / name).is_file() for name in plan_files | vulnerable_files)
    assert all(not (tmp_path / name).exists() for name in fixed_files)
    assert all(not (tmp_path / name).exists() for name in execution_files)

    for name in fixed_files | execution_files:
        (tmp_path / name).write_text("old", encoding="utf-8")
    runner.prepare_stage_entry("04_assertion_execute", tmp_path)

    assert all(
        (tmp_path / name).is_file()
        for name in plan_files | vulnerable_files | fixed_files
    )
    assert all(not (tmp_path / name).exists() for name in execution_files)
    assert not (tmp_path / "reachability_report.json").exists()

    runner.prepare_stage_entry("04_assertion_plan", tmp_path)
    assert all(
        not (tmp_path / name).exists()
        for name in plan_files | vulnerable_files | fixed_files
    )
    assert not (tmp_path / "reachability_report.json").exists()


def test_split_stage_failure_kinds_are_specific():
    assert runner.stage_failure_kind(
        "04_assertion_plan", 1, False, False
    ) == "assertion_plan_incomplete"
    assert runner.stage_failure_kind(
        "04_instrument_fixed", 1, True, False
    ) == "instrumentation_invalid"
    assert runner.stage_failure_kind(
        "04_assertion_execute", 1, True, False
    ) == "differential_unverified"
    assert runner.stage_failure_kind(
        "04_reachability", 1, False, False
    ) == "reachability_failed"


def test_assertion_execute_failure_writes_plan_feedback(tmp_path):
    runner.write_json(
        tmp_path / "assertion_results.json",
        {
            "sample_id": "sample",
            "differential_status": "probe_misplaced",
            "assertions": [
                {
                    "id": "A_root",
                    "kind": "required",
                    "verified": False,
                    "probe_placement_error": "probe fired before the fixed guard",
                    "matrix": {
                        "vulnerable": {
                            "original": {
                                "status": "violated",
                                "satisfied": False,
                                "left": 0,
                                "op": "ne",
                                "right": 0,
                            }
                        },
                        "fixed": {
                            "original": {
                                "status": "violated",
                                "satisfied": False,
                                "left": 0,
                                "op": "ne",
                                "right": 0,
                            }
                        },
                    },
                }
            ],
        },
    )

    out = runner.write_assertion_plan_feedback(tmp_path)

    assert out == tmp_path / "assertion_plan_feedback.md"
    text = out.read_text(encoding="utf-8")
    assert "A_root" in text
    assert "probe fired before the fixed guard" in text
    assert "Redesign the `required` root obligation" in text


def test_assertion_execute_incomplete_writes_execute_feedback(tmp_path):
    (tmp_path / "role_logs").mkdir()
    (tmp_path / "role_logs" / "04_assertion_execute.stdout.txt").write_text(
        "ran vulnerable only\n",
        encoding="utf-8",
    )
    (tmp_path / "role_logs" / "04_assertion_execute.stderr.txt").write_text(
        "",
        encoding="utf-8",
    )
    (tmp_path / "role_logs" / "04_assertion_execute.finalize.stderr.txt").write_text(
        "FileNotFoundError: fixed_assertion_trace.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "vulnerable_assertion_trace.txt").write_text(
        "CASE name=original\nENDCASE\n",
        encoding="utf-8",
    )

    out = runner.write_stage_retry_feedback("04_assertion_execute", tmp_path)

    assert out == tmp_path / "assertion_execute_feedback.md"
    text = out.read_text(encoding="utf-8")
    assert "execution completeness retry" in text
    assert "fixed_assertion_trace.txt" in text
    assert "Do not stop after the vulnerable side" in text
    assert not (tmp_path / "assertion_plan_feedback.md").exists()


def test_assertion_execute_guarded_without_perturbation_writes_execute_feedback(tmp_path):
    runner.write_json(
        tmp_path / "assertion_results.json",
        {
            "sample_id": "sample",
            "differential_status": "vulnerable_side_only",
            "assertions": [
                {
                    "id": "A_root",
                    "kind": "required",
                    "verified": False,
                    "verification_error": "fixed original is avoided",
                    "matrix": {
                        "vulnerable": {
                            "original": {
                                "status": "violated",
                                "satisfied": False,
                            }
                        },
                        "fixed": {
                            "original": {
                                "status": "avoided",
                                "satisfied": True,
                            }
                        },
                    },
                }
            ],
        },
    )
    runner.write_json(
        tmp_path / "perturbation_results.json",
        {
            "needed": True,
            "single_perturbation_attempt_recorded": False,
        },
    )
    result = runner.StageResult(
        name="04_assertion_execute",
        command="cmd",
        returncode=1,
        started_at="start",
        ended_at="end",
        stdout_path="",
        stderr_path="",
        required_outputs_ok=True,
        success_check_ok=False,
        failure_kind="differential_unverified",
    )

    out = runner.write_stage_retry_feedback(
        "04_assertion_execute",
        tmp_path,
        result=result,
    )

    assert out == tmp_path / "assertion_execute_feedback.md"
    assert "fixed original is avoided" in out.read_text(encoding="utf-8")
    assert "Missing Fixed Perturbation" in out.read_text(encoding="utf-8")
    assert not (tmp_path / "assertion_plan_feedback.md").exists()


def test_assertion_execute_differential_failure_writes_plan_feedback(tmp_path):
    runner.write_json(
        tmp_path / "assertion_results.json",
        {
            "sample_id": "sample",
            "differential_status": "failed",
            "assertions": [
                {
                    "id": "A_root",
                    "kind": "required",
                    "verified": False,
                    "matrix": {
                        "vulnerable": {
                            "original": {
                                "status": "satisfied",
                                "satisfied": True,
                            }
                        },
                        "fixed": {
                            "original": {
                                "status": "satisfied",
                                "satisfied": True,
                            }
                        },
                    },
                }
            ],
        },
    )
    result = runner.StageResult(
        name="04_assertion_execute",
        command="cmd",
        returncode=1,
        started_at="start",
        ended_at="end",
        stdout_path="",
        stderr_path="",
        required_outputs_ok=True,
        success_check_ok=False,
        failure_kind="differential_unverified",
    )

    out = runner.write_stage_retry_feedback(
        "04_assertion_execute",
        tmp_path,
        result=result,
    )

    assert out == tmp_path / "assertion_plan_feedback.md"
    assert "vulnerable" in out.read_text(encoding="utf-8")
    assert not (tmp_path / "assertion_execute_feedback.md").exists()


def test_differential_unverified_execute_does_not_retry_same_frozen_plan(tmp_path, monkeypatch):
    calls = []

    def fake_run_stage(**kwargs):
        calls.append(kwargs["stage"]["name"])
        return runner.StageResult(
            name="04_assertion_execute",
            command="cmd",
            returncode=1,
            started_at="start",
            ended_at="end",
            stdout_path="",
            stderr_path="",
            required_outputs_ok=True,
            success_check_ok=False,
            failure_kind="differential_unverified",
        )

    monkeypatch.setattr(runner, "run_stage", fake_run_stage)

    result = runner.run_stage_with_retries(
        stage={"name": "04_assertion_execute", "retries": 2},
        config={},
        result_dir=tmp_path,
    )

    assert result.failure_kind == "differential_unverified"
    assert calls == ["04_assertion_execute"]


def test_assertion_semantic_failure_reruns_full_stage04_chain(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    runner.write_json(state_path, {"stages": []})
    runner.write_json(
        tmp_path / "assertion_results.json",
        {
            "sample_id": "sample",
            "assertions": [
                {
                    "id": "A_root",
                    "kind": "required",
                    "verified": False,
                    "matrix": {
                        "vulnerable": {
                            "original": {
                                "status": "satisfied",
                                "satisfied": True,
                                "left": 1024,
                                "op": "ge",
                                "right": 1024,
                            }
                        },
                        "fixed": {
                            "original": {
                                "status": "satisfied",
                                "satisfied": True,
                                "left": 1024,
                                "op": "ge",
                                "right": 1024,
                            }
                        },
                    },
                }
            ],
        },
    )
    stages = [
        {"name": "04_assertion_plan"},
        {"name": "04_instrument_vulnerable"},
        {"name": "04_instrument_fixed"},
        {"name": "04_assertion_execute"},
    ]
    prepared = []
    calls = []

    def fake_prepare(stage_name, result_dir):
        prepared.append(stage_name)
        return []

    def fake_run_stage_with_retries(**kwargs):
        stage_name = kwargs["stage"]["name"]
        calls.append(stage_name)
        return runner.StageResult(
            name=stage_name,
            command="cmd",
            returncode=0,
            started_at="start",
            ended_at="end",
            stdout_path="",
            stderr_path="",
            required_outputs_ok=True,
            success_check_ok=True,
        )

    monkeypatch.setattr(runner, "prepare_stage_entry", fake_prepare)
    monkeypatch.setattr(runner, "run_stage_with_retries", fake_run_stage_with_retries)

    result = runner.run_assertion_semantic_repair_loop(
        initial_result=runner.StageResult(
            name="04_assertion_execute",
            command="cmd",
            returncode=1,
            started_at="start",
            ended_at="end",
            stdout_path="",
            stderr_path="",
            required_outputs_ok=True,
            success_check_ok=False,
            failure_kind="differential_unverified",
        ),
        stages=stages,
        state_path=state_path,
        stage_kwargs={"result_dir": tmp_path, "logs_dir": tmp_path, "config": {}},
    )

    assert result.ok is True
    assert calls == [
        "04_assertion_plan",
        "04_instrument_vulnerable",
        "04_instrument_fixed",
        "04_assertion_execute",
    ]
    assert prepared == calls
    assert "1024" in (tmp_path / "assertion_plan_feedback.md").read_text(encoding="utf-8")


def test_guarded_without_perturbation_failure_kind_is_execution_incomplete(tmp_path):
    runner.write_json(
        tmp_path / "perturbation_results.json",
        {
            "needed": True,
            "single_perturbation_attempt_recorded": False,
        },
    )

    assert runner.stage_failure_kind(
        "04_assertion_execute",
        0,
        True,
        False,
        tmp_path,
    ) == "assertion_execution_incomplete"


def test_guarded_without_vulnerable_violation_is_differential_unverified(tmp_path):
    runner.write_json(
        tmp_path / "assertion_results.json",
        {
            "original_case": "original",
            "assertions": [
                {
                    "id": "A_root",
                    "kind": "required",
                    "verified": False,
                    "verification_error": (
                        "fixed original is avoided; add exactly one perturbation "
                        "case before accepting the guarded fixed-side witness"
                    ),
                    "matrix": {
                        "vulnerable": {
                            "original": {
                                "status": "guarded",
                                "satisfied": True,
                            }
                        },
                        "fixed": {
                            "original": {
                                "status": "avoided",
                                "satisfied": True,
                            }
                        },
                    },
                }
            ],
        },
    )
    runner.write_json(
        tmp_path / "perturbation_results.json",
        {
            "needed": True,
            "single_perturbation_attempt_recorded": False,
        },
    )

    assert runner.stage_failure_kind(
        "04_assertion_execute",
        1,
        True,
        False,
        tmp_path,
    ) == "differential_unverified"


def test_repair_staging_failure_leaves_published_package_unchanged(tmp_path):
    published = tmp_path / "sample"
    published.mkdir()
    (published / "ground_truth.json").write_text("published")
    staging = runner.repair_staging_dir(published)

    runner.prepare_repair_staging(published, staging)
    (staging / "ground_truth.json").write_text("failed repair")

    assert (published / "ground_truth.json").read_text() == "published"
    assert (staging / "ground_truth.json").read_text() == "failed repair"


def test_explicit_repair_staging_reuse_preserves_accepted_earlier_stages(tmp_path):
    published = tmp_path / "sample"
    published.mkdir()
    (published / "ground_truth.json").write_text("published")
    staging = runner.repair_staging_dir(published)
    staging.mkdir()
    (staging / "ground_truth.json").write_text("accepted repaired GT")
    (staging / "static_review.json").write_text('{"static_valid": true}')

    runner.prepare_transactional_repair(published, staging, reuse=True)

    assert (staging / "ground_truth.json").read_text() == "accepted repaired GT"
    assert (staging / "static_review.json").is_file()


def test_explicit_repair_staging_reuse_requires_existing_directory(tmp_path):
    published = tmp_path / "sample"
    published.mkdir()

    with pytest.raises(SystemExit, match="Cannot reuse missing repair staging"):
        runner.prepare_transactional_repair(
            published,
            runner.repair_staging_dir(published),
            reuse=True,
        )


def test_generator_avoids_python38_path_apis():
    repo_root = Path(__file__).parents[1]
    for relative in (
        "gt_generation/runner.py",
        "gt_generation/gt_toolkit/compact_result.py",
        "gt_generation/gt_toolkit/prepare.py",
        "gt_generation/gt_toolkit/reachability.py",
        "evaluator/reachability/arvo_gdb.py",
    ):
        source = (repo_root / relative).read_text(encoding="utf-8")
        assert "missing_ok=" not in source
        assert ".removeprefix(" not in source
        assert ".removesuffix(" not in source
        assert ".is_relative_to(" not in source


def test_arvo_reachability_uses_host_orchestrator_not_image_python():
    source = (
        Path(__file__).parents[1]
        / "gt_generation"
        / "gt_toolkit"
        / "reachability.py"
    ).read_text(encoding="utf-8")
    function = source.split("def run_for_arvo(", 1)[1].split(
        "\ndef _proxy_environment", 1
    )[0]

    assert "prepare_arvo_target(" in function
    assert "run_arvo_gdb(" in function
    assert "python3 -m gt_toolkit" not in function


def test_successful_repair_staging_atomically_replaces_package(tmp_path):
    published = tmp_path / "sample"
    published.mkdir()
    (published / "ground_truth.json").write_text("published")
    staging = runner.repair_staging_dir(published)
    runner.prepare_repair_staging(published, staging)
    (staging / "ground_truth.json").write_text("validated repair")

    runner.publish_repair_staging(staging, published)

    assert (published / "ground_truth.json").read_text() == "validated repair"
    assert not staging.exists()
    assert not published.with_name("sample.repair-backup").exists()


def test_repair_publish_gate_requires_commitment(tmp_path):
    assert runner.repair_package_ready_to_publish(tmp_path) is False


def test_stage_bounds_reject_unknown_and_reversed_ranges():
    stages = [{"name": "02"}, {"name": "03"}, {"name": "05"}]

    with pytest.raises(SystemExit, match="unknown stage"):
        runner.validate_stage_bounds(stages, "missing", "", "")
    with pytest.raises(SystemExit, match="must not follow"):
        runner.validate_stage_bounds(stages, "05", "02", "")


def test_verified_graph_cannot_add_or_move_candidate_invariants():
    candidate = {
        "root_cause_criterion": {"invariant_id": "root"},
        "nodes": [
            {
                "invariant_id": "root",
                "role": "root_cause",
                "file": "src/a.c",
                "function": "parse",
                "line": 10,
                "operands": ["length", "capacity"],
                "relation": {"op": "le", "left": "length", "right": "capacity"},
                "verified": True,
            },
            {
                "invariant_id": "sink",
                "role": "sink",
                "file": "src/a.c",
                "function": "parse",
                "line": 20,
                "operands": ["buf"],
                "relation": {"op": "same_object", "left": "buf", "right": "buf"},
                "verified": True,
            }
        ],
        "edges": [],
    }
    verified = {
        **candidate,
        "nodes": [],
    }

    assert runner.verified_graph_is_candidate_subset(candidate, verified) is False
    verified["nodes"] = [
        {
            "invariant_id": "sink",
            "file": "tests/fuzz/a_fuzzer.c",
            "function": "LLVMFuzzerTestOneInput",
            "line": 20,
        }
    ]
    assert runner.verified_graph_is_candidate_subset(candidate, verified) is False
    verified["nodes"] = []
    verified["nodes"] = [
        {
            **candidate["nodes"][0],
            "relation": {"op": "gt", "left": "length", "right": "capacity"},
            "verified_by": "assertion.root",
        }
    ]
    assert runner.verified_graph_is_candidate_subset(candidate, verified) is False
    verified["nodes"] = [{**candidate["nodes"][0], "verified_by": "assertion.root"}]
    assert runner.verified_graph_is_candidate_subset(candidate, verified) is True


def test_assertion_execute_restores_mutated_frozen_inputs(tmp_path):
    original = {"nodes": [{"invariant_id": "root", "role": "root_cause"}]}
    runner.write_json(tmp_path / "candidate_invariants.json", original)
    for name in (
        "candidate_assertions.json",
        "field_bindings.json",
        "event_locations.json",
        ".assertion_spec_frozen.json",
        "assertion_preflight.json",
    ):
        (tmp_path / name).write_text(name, encoding="utf-8")

    snapshot = runner.snapshot_assertion_plan_inputs(tmp_path)
    runner.write_json(tmp_path / "candidate_invariants.json", {"nodes": []})

    report = runner.restore_modified_assertion_plan_inputs(tmp_path, snapshot)

    assert report["ok"] is False
    assert report["modified_files"][0]["file"] == "candidate_invariants.json"
    assert runner.load_json(tmp_path / "candidate_invariants.json") == original


def test_failed_atomic_publish_restores_original_package(tmp_path, monkeypatch):
    published = tmp_path / "sample"
    published.mkdir()
    (published / "ground_truth.json").write_text("published")
    staging = runner.repair_staging_dir(published)
    staging.mkdir()
    (staging / "ground_truth.json").write_text("repair")
    real_replace = os.replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(runner.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="simulated publish failure"):
        runner.publish_repair_staging(staging, published)

    assert (published / "ground_truth.json").read_text() == "published"
    assert (staging / "ground_truth.json").read_text() == "repair"
    assert not published.with_name("sample.repair-backup").exists()


def test_failed_review_reopens_fresh_producer_and_reviewer_sessions(tmp_path, monkeypatch):
    producer = {"name": "02_fine_trace", "role": "roles/02_fine_trace.md"}
    review = {
        "name": "03_trace_review",
        "role": "roles/03_trace_review.md",
        "incremental_role": "roles/03_trace_review_incremental.md",
        "feedback_to": "02_fine_trace",
        "feedback_rounds": 2,
    }
    state_path = tmp_path / "state.json"
    logs_dir = tmp_path / "role_logs"
    logs_dir.mkdir()
    runner.write_json(tmp_path / "ground_truth.json", {"fine_trace": [{"step": 1}]})
    runner.write_json(state_path, {"stages": []})
    calls = []

    def fake_run_stage_with_retries(**kwargs):
        stage = kwargs["stage"]
        calls.append((stage["name"], stage.get("_log_suffix")))
        if stage["name"] == "02_fine_trace":
            runner.write_json(
                tmp_path / "ground_truth.json", {"fine_trace": [{"step": 1}, {"step": 2}]}
            )
        return _result(stage["name"], ok=True)

    monkeypatch.setattr(runner, "run_stage_with_retries", fake_run_stage_with_retries)
    result = runner.run_feedback_loop(
        review_stage=review,
        initial_result=_result("03_trace_review", ok=False),
        stages=[producer, review],
        state_path=state_path,
        stage_kwargs={"stage": review, "result_dir": tmp_path, "logs_dir": logs_dir},
    )

    assert result.ok is True
    assert calls == [
        ("02_fine_trace", "feedback_1"),
        ("03_trace_review", "incremental_feedback_1"),
        ("03_trace_review", "final_feedback_1"),
    ]
    assert [item["name"] for item in runner.load_json(state_path)["stages"]] == [
        "02_fine_trace",
        "03_trace_review",
        "03_trace_review",
    ]
    delta = runner.load_json(logs_dir / "ground_truth.delta_feedback_1.json")
    assert delta["changed_paths"] == ["$.fine_trace.length", "$.fine_trace[1]"]


def test_runtime_feedback_routes_repair_to_conditional_dynamic_role(tmp_path, monkeypatch):
    producer = {"name": "02_fine_trace", "role": "roles/02_fine_trace.md"}
    review = {
        "name": "03_trace_review",
        "role": "roles/03_trace_review.md",
        "runtime_role": "roles/02_runtime_disambiguation.md",
        "feedback_to": "02_fine_trace",
        "feedback_rounds": 1,
    }
    state_path = tmp_path / "state.json"
    logs_dir = tmp_path / "role_logs"
    logs_dir.mkdir()
    runner.write_json(tmp_path / "ground_truth.json", {"fine_trace": [{"step": 1}]})
    runner.write_json(
        tmp_path / "trace_feedback.json",
        {
            "needs_revision": True,
            "needs_runtime_disambiguation": True,
            "observe": "correlate the producer and consumer offsets",
            "issues": [],
        },
    )
    runner.write_json(state_path, {"stages": []})
    calls = []

    def fake_run_stage_with_retries(**kwargs):
        stage = kwargs["stage"]
        calls.append((stage["name"], stage["role"], stage.get("_log_suffix")))
        if stage["name"] == "02_runtime_disambiguation":
            runner.write_json(
                tmp_path / "ground_truth.json",
                {"fine_trace": [{"step": 1, "note": "correlated at runtime"}]},
            )
        return _result(stage["name"], ok=True)

    monkeypatch.setattr(runner, "run_stage_with_retries", fake_run_stage_with_retries)
    result = runner.run_feedback_loop(
        review_stage=review,
        initial_result=_result("03_trace_review", ok=False),
        stages=[producer, review],
        state_path=state_path,
        stage_kwargs={"stage": review, "result_dir": tmp_path, "logs_dir": logs_dir},
    )

    assert result.ok is True
    assert calls == [
        (
            "02_runtime_disambiguation",
            "roles/02_runtime_disambiguation.md",
            "feedback_1",
        ),
        ("03_trace_review", "roles/03_trace_review.md", "feedback_1"),
    ]


def test_dry_run_has_separate_state_file(tmp_path):
    assert runner.generation_state_path(tmp_path, dry_run=False).name == "gt_generation_state.json"
    assert runner.generation_state_path(tmp_path, dry_run=True).name == "gt_generation_state.dry_run.json"
    assert runner.generation_logs_path(tmp_path, dry_run=False).name == "role_logs"
    assert runner.generation_logs_path(tmp_path, dry_run=True).name == "role_logs_dry_run"


def test_resumed_agent_stage_requires_source_hydration():
    stages = [
        {"name": "03_trace_review", "role": "roles/03_trace_review.md"},
        {"name": "05_validate"},
    ]

    assert runner.needs_resume_source_hydration(stages) is True
    assert runner.needs_resume_source_hydration(stages, dry_run=True) is False
    assert runner.needs_resume_source_hydration([{"name": "05_validate"}]) is False
    assert runner.needs_resume_source_hydration(
        [{"name": "00_prepare"}, *stages]
    ) is False


def test_new_review_run_clears_only_stale_feedback_control_files(tmp_path):
    logs = tmp_path / "role_logs"
    logs.mkdir()
    stale = [
        logs / "ground_truth.before_feedback_1.json",
        logs / "ground_truth.delta_feedback_2.json",
    ]
    preserved = [
        logs / "03_trace_review.stdout.txt",
        logs / "03_trace_review.incremental_feedback_1.stderr.txt",
        logs / "unrelated.json",
    ]
    for path in [*stale, *preserved]:
        path.write_text("{}")

    removed = runner.clear_stale_feedback_control_files(logs)

    assert set(removed) == set(stale)
    assert all(not path.exists() for path in stale)
    assert all(path.exists() for path in preserved)


def test_resume_skip_requires_current_stage_gate(tmp_path):
    stage = {
        "name": "03_trace_review",
        "required_outputs": [
            "{result_dir}/static_review.json",
            "{result_dir}/trace_feedback.json",
        ],
        "success_check": {
            "path": "{result_dir}/static_review.json",
            "all": [
                {"field": "static_valid", "equals": True},
                {"field": "trace_complete", "equals": True},
                {"field": "local_transitions_closed", "equals": True},
                {"field": "global_causal_chain_closed", "equals": True},
            ],
        },
    }
    runner.write_json(
        tmp_path / "static_review.json",
        {
            "static_valid": False,
            "trace_complete": True,
            "local_transitions_closed": True,
            "global_causal_chain_closed": True,
        },
    )
    runner.write_json(tmp_path / "trace_feedback.json", {"needs_revision": True})

    resumable = runner.current_resumable_ok_stages(
        stages=[stage],
        prior_ok={"03_trace_review"},
        config={"vars": {}},
        sample={},
        sample_path=tmp_path / "sample.json",
        sample_id="sample",
        repo_root=tmp_path,
        code_root=Path(__file__).parents[1] / "gt_generation",
        result_dir=tmp_path,
    )

    assert resumable == set()


def test_partial_rerun_timing_keeps_latest_duration_per_stage():
    prior = [
        {"name": "02_fine_trace", "duration_seconds": 10.0, "ok": True},
        {"name": "04_assertion_validator", "duration_seconds": 30.0, "ok": False},
    ]
    current = [
        {"name": "04_assertion_validator", "duration_seconds": 20.0, "ok": True},
        {"name": "05_validate", "duration_seconds": 0.2, "ok": True},
    ]

    assert runner.merge_stage_timings(prior, current) == [
        {"name": "02_fine_trace", "duration_seconds": 10.0, "ok": True},
        {"name": "04_assertion_validator", "duration_seconds": 20.0, "ok": True},
        {"name": "05_validate", "duration_seconds": 0.2, "ok": True},
    ]
