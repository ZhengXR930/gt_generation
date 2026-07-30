import json
from pathlib import Path

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


def test_workflow_uses_four_isolated_agent_stages_and_review_feedback():
    workflow = json.loads(
        (Path(__file__).parents[1] / "gt_generation" / "workflow.json").read_text()
    )
    assert workflow["compact_on_success"] is True
    agent_stages = [stage for stage in workflow["stages"] if stage.get("role")]

    assert [stage["name"] for stage in agent_stages] == [
        "01_reproducer",
        "02_fine_trace",
        "03_trace_review",
        "04_assertion_validator",
    ]
    assert len({stage["role"] for stage in agent_stages}) == 4
    review = next(stage for stage in agent_stages if stage["name"] == "03_trace_review")
    assert review["feedback_to"] == "02_fine_trace"
    assert review["runtime_role"] == "roles/02_runtime_disambiguation.md"
    assert review["incremental_role"] == "roles/03_trace_review_incremental.md"
    assert review["feedback_rounds"] == 2
    assertion_stage = next(
        stage for stage in agent_stages if stage["name"] == "04_assertion_validator"
    )
    assert assertion_stage["success_check"] == {
        "path": "{result_dir}/assertion_results.json",
        "field": "all_verified",
        "equals": True,
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
