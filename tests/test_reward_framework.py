import json
import os
import urllib.request
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

from reward_framework.adapters.base import CallbackAdapter
from reward_framework.models import (
    EvidenceState,
    ProbePlan,
    RawRuntimeReport,
    RewardSpec,
    RuntimeFact,
    SourceAnchor,
    StageStatus,
)
from reward_framework.orchestrator import RewardFramework
from reward_framework.openhands_entrypoint import _is_iteration_limit_error
from reward_framework.openhands_entrypoint import _codex_auth_file
from openhands.core.schema import AgentState
from reward_framework.cross_sample import CrossSampleHarnessPatcher
from reward_framework.episode_analyzer import (
    EpisodeAnalyzer,
    _assessment_progress_key,
    build_stage_control,
    collect_control_plane_transactions,
    collect_episode_metrics,
    collect_protocol_evidence,
)
from reward_framework.experience_pool import ExperiencePool
from reward_framework.harness_repository import HarnessRepository
from reward_framework.runtime import StaticInstrumentationBackend
from reward_framework.runtime import default_trigger_oracle
from reward_framework.adapters.openhands import (
    OpenHandsAdapter,
    OpenHandsRewardTransport,
    _SubjectWallClockTimeout,
    _call_with_wall_timeout,
    _submission_bridge_host,
    is_direct_submit_invocation,
    is_fine_trace_finalization,
    fine_trace_finalization_trigger,
)
from reward_framework.instrumentation.arvo import (
    ArvoGDBInstrumentationBackend,
    compile_checkpoints,
)
from reward_framework.models import Probe
from reward_framework.stage_evaluator import evaluate_stages
from reward_framework.submission_tool import resolve_workspace_path
from reward_framework.source_view import eligible_source_files
from reward_framework.source_view import resolve_public_source_path
from reward_framework.spec_agent import SpecAgent, canonicalize_spec_sources
from reward_framework.feedback_agent import FeedbackAgent, fallback_feedback
from reward_framework.backend import CodexBackend
from poc_generation.poc_generator.run_sample import persist_reward_framework_state
from poc_generation.openhands_fine_trace_main import (
    _is_submit_command,
    _submitted_trace,
)
from poc_generation.poc_generator.run_openhands_cybergym import (
    configure_harness_profile,
    reward_subject_llm_recovery_config,
)


class FakeBackend:
    model = "fake-codex"

    def __init__(self):
        self.roles = []

    def run_json(self, *, role, prompt, schema, cwd):
        self.roles.append(role)
        if role == "initialize_spec":
            anchor = {
                "file": "parser.c", "function": "parse_input",
                "fact": "The parser accepts candidate bytes."
            }
            return {
                "claims": {
                    "admission": "The parser accepts candidate bytes.",
                    "source": "The candidate controls length.",
                    "root": "Length exceeds capacity.",
                    "propagation": "Length reaches the copy.",
                    "target": "The copy consumes the oversized length.",
                },
                "evidence": {stage: [anchor] for stage in (
                    "admission", "source", "root", "propagation", "target"
                )},
            }
        if role == "observe_trajectory":
            return {"decision": "request_submission"}
        if role == "design_probes":
            return {
                "probes": [{
                    "stage": "root", "anchor_kind": "issue",
                    "file": "parser.c", "function": "parse_input",
                    "statement": "if (length > capacity)",
                    "captures": ["length", "capacity"],
                    "condition": "length > capacity",
                    "purpose": "Observe the vulnerable relation."
                }],
                "trace_claims": ["The candidate claims length exceeds capacity."],
            }
        if role == "generate_feedback":
            return {
                "summary": "Admission and Source were confirmed; Root was refuted by the observed relation.",
                "contradiction": "The candidate Root claim was refuted by runtime fact ROOT-FALSE.",
                "delta": "This is the first distinct candidate.",
                "evidence_ids": ["ROOT-FALSE"],
            }
        if role == "analyze_episode":
            bundle = json.loads((Path(cwd) / "episode_bundle.json").read_text())
            sequence = bundle["trajectory"]["events"][-1]["sequence"]
            return {
                "assessment": "subject_limited",
                "experiences": [{
                    "kind": "subject_failure", "category": "causal_stagnation",
                    "confidence": "medium", "evidence_sequences": [sequence],
                }],
            }
        raise AssertionError(role)


def test_only_answering_fine_trace_state_bypasses_reward_finish_guard():
    assert is_fine_trace_finalization(SimpleNamespace(extra_data={
        "fine_trace_finalization": {"status": "answering"}
    }))
    assert not is_fine_trace_finalization(SimpleNamespace(extra_data={
        "fine_trace_finalization": {"status": "completed"}
    }))
    assert not is_fine_trace_finalization(SimpleNamespace(extra_data={}))
    state = SimpleNamespace(extra_data={
        "fine_trace_finalization": {
            "status": "answering", "trigger": "iteration_limit"
        }
    })
    assert fine_trace_finalization_trigger(state) == "iteration_limit"
    assert fine_trace_finalization_trigger(SimpleNamespace(extra_data={})) is None


def test_openhands_submission_readiness_requires_poc_and_trace(tmp_path):
    adapter = OpenHandsAdapter(
        workspace_root=tmp_path,
        inject=lambda _: None,
        checkpoint_callback=lambda _: None,
    )
    assert adapter.submission_ready() is False
    (tmp_path / "candidate_trace.json").write_text("[]", encoding="utf-8")
    assert adapter.submission_ready() is False
    (tmp_path / "poc.bin").write_bytes(b"candidate")
    assert adapter.submission_ready() is True
    assert adapter.submission_fingerprint() is not None


def test_supervisor_does_not_request_unchanged_submitted_candidate(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text("int parse_input(void) { return 0; }\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    poc = workspace / "poc.bin"
    trace = workspace / "candidate_trace.json"
    poc.write_bytes(b"candidate")
    trace.write_text("[]", encoding="utf-8")
    injected = []
    backend = FakeBackend()
    framework = RewardFramework.create(
        task_id="unchanged-candidate",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=backend,
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=OpenHandsAdapter(
            workspace_root=workspace,
            inject=injected.append,
            checkpoint_callback=lambda _: None,
        ),
    )
    framework.record_event(
        source="agent", kind="ActionType.RUN", payload={"message": "write candidate"}
    )
    assert framework.observe_trajectory() == "request_submission"
    framework.store.register_candidate(poc_path=poc, trace_path=trace)
    state = framework.store.load_observation()
    state.submission_requested = False
    framework.store.save_observation(state)
    framework.record_event(
        source="agent", kind="ActionType.MESSAGE", payload={"message": "same candidate"}
    )
    assert framework.observe_trajectory() == "continue"
    assert len(injected) == 1


def test_feedback_file_error_uses_deterministic_fallback(tmp_path):
    class MissingOutputBackend:
        model = "missing-output"

        def run_json(self, **_kwargs):
            raise FileNotFoundError("missing isolated output")

    assessment = evaluate_stages(
        RewardSpec(
            {stage: stage for stage in (
                "admission", "source", "root", "propagation", "target"
            )},
            {stage: (SourceAnchor("parser.c", "parse_input", stage),) for stage in (
                "admission", "source", "root", "propagation", "target"
            )},
        ),
        RawRuntimeReport(0, "", "", False, {}, (), True),
    )
    feedback = FeedbackAgent(MissingOutputBackend()).generate(
        report=RawRuntimeReport(0, "", "", False, {}, (), True),
        assessment=assessment,
        previous=None,
        agent_root=tmp_path,
    )
    assert feedback.summary.startswith("Confirmed causal prefix:")


def test_control_plane_projection_is_value_free_and_links_reward_lifecycle():
    events = [
        {"sequence": 1, "source": "reward_agent", "kind": "submission_requested", "payload": {}},
        {"sequence": 2, "source": "agent", "kind": "ActionType.RUN", "payload": {
            "tool_call_metadata": {"function_name": "submit_candidate", "tool_call_id": "call"},
            "args": {"blocking": True, "command": "SECRET PATH"},
        }},
        {"sequence": 3, "source": "coding_agent", "kind": "candidate_submitted", "payload": {
            "attempt_number": 1, "candidate_id": "SECRET", "duplicate_of": None,
        }},
        {"sequence": 4, "source": "reward_agent", "kind": "verification_completed", "payload": {
            "attempt_number": 1, "runtime_facts": [{"statement": "SECRET SOURCE"}],
        }},
        {"sequence": 5, "source": "user", "kind": "ActionType.MESSAGE", "payload": {
            "message": "[Runtime reward evidence] SECRET SEMANTICS",
        }},
        {"sequence": 6, "source": "agent", "kind": "ActionType.RUN", "payload": {
            "tool_call_metadata": {"function_name": "execute_bash"},
        }},
        {"sequence": 7, "source": "agent", "kind": "ObservationType.RUN", "payload": {
            "cause": 2,
            "content": "{\"error\": \"FileNotFoundError: SECRET PATH\"}",
            "tool_call_metadata": {"function_name": "submit_candidate"},
            "extras": {"metadata": {"exit_code": 0}},
        }},
    ]
    projected = collect_control_plane_transactions(events)
    encoded = json.dumps(projected)
    assert "SECRET" not in encoded
    assert projected["submission_transactions"] == [{
        "attempt_number": 1,
        "submission_sequence": 3,
        "reminder_preceded_submission": True,
        "candidate_registered": True,
        "duplicate_candidate": False,
        "verification_completed": True,
        "reward_delivered": True,
        "subject_acted_after_reward_before_next_submission": True,
        "next_submission_is_distinct": None,
    }]
    assert projected["submission_tool_results"][0]["exit_class"] == "success"
    assert projected["submission_tool_results"][0]["error_type"] == "FileNotFoundError"


def test_stage_gate_blocks_later_confirmed_stage():
    anchor = SourceAnchor("a.c", "f", "fact")
    spec = RewardSpec(
        {stage: stage for stage in ("admission", "source", "root", "propagation", "target")},
        {stage: (anchor,) for stage in ("admission", "source", "root", "propagation", "target")},
    )
    report = RawRuntimeReport(
        exit_code=0, stdout="", stderr="", trigger_observed=False,
        stage_observations={
            "admission": StageStatus.CONFIRMED,
            "source": StageStatus.CONFIRMED,
            "root": StageStatus.REFUTED,
            "propagation": StageStatus.CONFIRMED,
            "target": StageStatus.CONFIRMED,
        },
        facts=(), instrumentation_available=True,
    )
    result = evaluate_stages(spec, report)
    assert result.longest_confirmed_prefix == ("admission", "source")
    assert result.first_unresolved == "root"
    assert result.stages["root"] == StageStatus.REFUTED
    assert result.stages["propagation"] == StageStatus.OBSERVED_BUT_BLOCKED
    assert result.stages["target"] == StageStatus.OBSERVED_BUT_BLOCKED


def test_causal_progress_counts_within_stage_runtime_movement():
    not_reached = _assessment_progress_key({"stages": {
        "admission": "confirmed", "source": "not_reached",
        "root": "not_reached", "propagation": "not_reached",
        "target": "not_reached",
    }})
    unresolved = _assessment_progress_key({"stages": {
        "admission": "confirmed", "source": "unresolved",
        "root": "not_reached", "propagation": "not_reached",
        "target": "not_reached",
    }})
    refuted = _assessment_progress_key({"stages": {
        "admission": "confirmed", "source": "refuted",
        "root": "not_reached", "propagation": "not_reached",
        "target": "not_reached",
    }})
    confirmed = _assessment_progress_key({"stages": {
        "admission": "confirmed", "source": "confirmed",
        "root": "not_reached", "propagation": "not_reached",
        "target": "not_reached",
    }})
    assert not_reached < unresolved < confirmed
    assert refuted == not_reached


def test_codex_backend_uses_one_persistent_session_per_instance(tmp_path, monkeypatch):
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    cwd = tmp_path / "view"
    cwd.mkdir()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"decision":"continue"}', encoding="utf-8")
        started = (
            '{"type":"thread.started","thread_id":"thread-123"}\n'
            if len(calls) == 1 else ""
        )
        return SimpleNamespace(
            returncode=0, stderr="", stdout=started +
            '{"type":"turn.completed","usage":{"input_tokens":1}}\n'
        )

    monkeypatch.setattr("reward_framework.backend.subprocess.run", fake_run)
    session_file = tmp_path / "sessions/reward.session"
    backend = CodexBackend(session_file=session_file)
    assert backend.run_json(
        role="initialize_spec", prompt="one", schema=schema, cwd=cwd
    )["decision"] == "continue"
    assert session_file.read_text().strip() == "thread-123"
    backend.run_json(role="observe_trajectory", prompt="two", schema=schema, cwd=cwd)
    assert calls[0][1] == "exec"
    assert calls[1][1:3] == ["exec", "resume"]
    assert "thread-123" in calls[1]
    assert backend.runs[0].resumed is False
    assert backend.runs[1].resumed is True


def test_codex_backend_can_use_external_state_in_fresh_turns(tmp_path, monkeypatch):
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    cwd = tmp_path / "view"
    cwd.mkdir()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"decision":"continue"}', encoding="utf-8")
        thread = f"thread-{len(calls)}"
        return SimpleNamespace(
            returncode=0, stderr="", stdout=(
                f'{{"type":"thread.started","thread_id":"{thread}"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":1}}\n'
            ),
        )

    monkeypatch.setattr("reward_framework.backend.subprocess.run", fake_run)
    backend = CodexBackend(
        session_file=tmp_path / "sessions/reward.session",
        fresh_each_run=True,
    )
    backend.run_json(role="design_probes", prompt="one", schema=schema, cwd=cwd)
    backend.run_json(role="generate_feedback", prompt="two", schema=schema, cwd=cwd)
    assert calls[0][1] == "exec"
    assert calls[1][1] == "exec"
    assert "resume" not in calls[1]
    assert backend.runs[0].resumed is False
    assert backend.runs[1].resumed is False
    assert backend.session_id == "thread-2"
    assert "--ephemeral" in calls[0]
    assert "--ephemeral" in calls[1]
    assert not (tmp_path / "sessions/reward.session").exists()


def test_codex_backend_isolation_mounts_only_explicit_role_view(tmp_path):
    executable = tmp_path / ".codex/packages/standalone/current/bin/codex"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    auth = tmp_path / ".codex/auth.json"
    auth.write_text("{}", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    view = tmp_path / "agent_view"
    view.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    backend = CodexBackend(
        executable=str(executable), fresh_each_run=True,
        isolation_image="reward-controller:test",
        isolation_auth_file=auth,
    )
    command = backend._isolated_command(
        cwd=view.resolve(), schema=schema, output_root=output.resolve()
    )
    serialized = " ".join(command)
    assert f"src={view.resolve()},dst=/work,readonly" in serialized
    assert f"src={auth.resolve()},dst=/root/.codex/auth.json,readonly" in serialized
    assert "dst=/home/xinran" not in serialized
    assert "/var/run/docker.sock" not in serialized
    assert "--ephemeral" in command
    for feature in ("apps", "plugins", "goals", "multi_agent", "browser_use"):
        assert any(
            command[index:index + 2] == ["--disable", feature]
            for index in range(len(command) - 1)
        )
    sandbox_index = command.index("--sandbox")
    assert command[sandbox_index + 1] == "danger-full-access"


def test_codex_auth_is_derived_from_executable_package(tmp_path):
    executable = tmp_path / ".codex/packages/standalone/current/bin/codex"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    auth = tmp_path / ".codex/auth.json"
    auth.write_text("{}", encoding="utf-8")
    assert _codex_auth_file(str(executable)) == auth.resolve()


def test_unavailable_instrumentation_is_unresolved_not_failure():
    anchor = SourceAnchor("a.c", "f", "fact")
    spec = RewardSpec(
        {stage: stage for stage in ("admission", "source", "root", "propagation", "target")},
        {stage: (anchor,) for stage in ("admission", "source", "root", "propagation", "target")},
    )
    report = RawRuntimeReport(0, "", "", False, {}, (), False)
    result = evaluate_stages(spec, report)
    assert result.stages["admission"] == StageStatus.UNRESOLVED
    assert result.stages["source"] == StageStatus.NOT_REACHED


def test_trigger_feedback_distinguishes_success_from_incomplete_stage_attribution():
    anchor = SourceAnchor("a.c", "f", "fact")
    spec = RewardSpec(
        {stage: stage for stage in ("admission", "source", "root", "propagation", "target")},
        {stage: (anchor,) for stage in ("admission", "source", "root", "propagation", "target")},
    )
    report = RawRuntimeReport(
        77, "MemorySanitizer", "", True,
        {
            "admission": StageStatus.CONFIRMED,
            "source": StageStatus.CONFIRMED,
            "root": StageStatus.CONFIRMED,
            "target": StageStatus.CONFIRMED,
        },
        (), True,
    )
    assessment = evaluate_stages(spec, report)
    feedback = fallback_feedback(report, assessment, "first")
    assert assessment.first_unresolved == "propagation"
    assert assessment.stages["target"] == StageStatus.OBSERVED_BUT_BLOCKED
    assert "independent trigger is confirmed" in feedback.summary
    assert "stage attribution" in feedback.summary


def test_generic_exit_one_is_not_trigger_and_paths_cannot_escape(tmp_path):
    assert default_trigger_oracle(1, "candidate rejected", "") is False
    assert default_trigger_oracle(139, "", "") is True
    assert default_trigger_oracle(77, "MemorySanitizer: use-of-uninitialized-value", "") is True
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for value in ("../poc", "/tmp/poc", "/workspace/../tmp/poc"):
        try:
            resolve_workspace_path(workspace, value, "poc_path")
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe workspace path accepted: {value}")


def test_end_to_end_state_submission_and_dedup(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse_input(int length, int capacity) {\n"
        "  if (length > capacity) return -1;\n"
        "  return 0;\n}\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "poc.bin").write_bytes(b"candidate")
    (workspace / "trace.json").write_text(
        json.dumps([{"file": "parser.c", "function": "parse_input"}]),
        encoding="utf-8",
    )
    injected = []
    checkpoints = []
    adapter = CallbackAdapter(
        workspace_root=workspace,
        inject=injected.append,
        checkpoint_callback=lambda label: checkpoints.append(label) or workspace / label,
    )
    runtime = StaticInstrumentationBackend(RawRuntimeReport(
        exit_code=0,
        stdout="accepted",
        stderr="",
        trigger_observed=False,
        stage_observations={
            "admission": StageStatus.CONFIRMED,
            "source": StageStatus.CONFIRMED,
            "root": StageStatus.REFUTED,
            "propagation": StageStatus.CONFIRMED,
            "target": StageStatus.NOT_REACHED,
        },
        facts=(RuntimeFact(
            fact_id="ROOT-FALSE", stage="root", kind="predicate",
            statement="Observed length did not exceed capacity.",
        ),),
        instrumentation_available=True,
    ))
    backend = FakeBackend()
    framework = RewardFramework.create(
        task_id="sample",
        issue_description="A parser copies an oversized candidate length.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=backend,
        instrumentation=runtime,
        platform=adapter,
    )
    framework.record_event(
        source="coding_agent", kind="tool_result",
        payload={"command": "created poc.bin", "exit_code": 0},
    )
    assert framework.observe_trajectory() == "request_submission"
    assert injected[0].startswith("[External trajectory observer]")

    first = framework.submit_candidate({
        "poc_path": "poc.bin", "trace_path": "trace.json"
    })
    assert first["triggered"] is False
    assert first["assessment"]["longest_confirmed_prefix"] == ["admission", "source"]
    assert first["assessment"]["stages"]["propagation"] == "observed_but_blocked"
    assert first["candidate_stats"] == {
        "total_submissions": 1, "unique_candidates": 1,
        "duplicate_submissions": 0, "unique_ratio": 1.0,
    }
    assert checkpoints == ["submission_0001"]
    assert (tmp_path / "state/candidates" / first["candidate_id"] / "poc").is_file()
    assert (tmp_path / "state/evidence/attempt_0001.json").is_file()

    second = framework.submit_candidate({
        "poc_path": "/workspace/poc.bin", "trace_path": "/workspace/trace.json"
    })
    assert second["duplicate_of"] == first["candidate_id"]
    assert second["candidate_stats"]["total_submissions"] == 2
    assert second["candidate_stats"]["unique_candidates"] == 1
    assert second["candidate_stats"]["unique_ratio"] == 0.5
    assert len(runtime.calls) == 2
    assert framework.status()["terminal_reason"] is None


def test_episode_harness_is_frozen_and_emits_cross_sample_experience(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse_input(int length, int capacity) {\n"
        "  if (length > capacity) return -1;\n"
        "  return 0;\n}\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "poc.bin").write_bytes(b"candidate")
    (workspace / "trace.json").write_text("[]", encoding="utf-8")
    injected = []
    backend = FakeBackend()
    framework = RewardFramework.create(
        task_id="adaptive",
        issue_description="An oversized parser length reaches a copy.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=backend,
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False,
            {"admission": StageStatus.CONFIRMED,
             "source": StageStatus.CONFIRMED,
             "root": StageStatus.REFUTED},
            (RuntimeFact("ROOT-FALSE", "root", "predicate", "root false"),),
            True,
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=injected.append),
        baseline_profile="openhands_0.33.0_pristine",
        harness_version=7,
    )
    framework.record_event(
        source="controller", kind="observer_submission_deferred",
        payload={"reason": "candidate trace has not been materialized"},
    )
    framework.record_event(
        source="reward_agent", kind="submission_requested",
        payload={"message": "submit the ready candidate"},
    )
    framework.submit_candidate({"poc_path": "poc.bin", "trace_path": "trace.json"})
    framework.record_event(
        source="coding_agent", kind="tool_result",
        payload={"command": "revised candidate", "exit_code": 0},
    )

    state = tmp_path / "state"
    assert (state / "trajectory_state.json").is_file()
    assert (state / "evidence_state.json").is_file()
    assert (state / "harness_state.json").is_file()
    global_state = json.loads((state / "agent_view/global_state.json").read_text())
    assert global_state["evidence"]["latest_attempt_number"] == 1
    assert global_state["evidence"]["recurring_errors"]["causal_boundary:root"] == 1
    assert global_state["harness"]["baseline_profile"] == "openhands_0.33.0_pristine"
    assert global_state["harness"]["active_program_version"] == 7
    assert any(message.startswith("[Runtime reward evidence]") for message in injected)

    metrics = collect_episode_metrics(framework.store, harness_version=7)
    assert metrics.total_submissions == 1
    assert metrics.unique_candidates == 1
    assert metrics.reward_events == 1
    assert metrics.supervisor_reminders == 2
    assert metrics.materialization_reminders == 1
    assert metrics.ready_submission_reminders == 1
    assert metrics.reminder_triggered_submissions == 1
    assert metrics.post_reminder_no_submission is False
    experience = EpisodeAnalyzer(backend).analyze(
        store=framework.store, harness_version=7
    )
    assert experience["stage_control"]["first_blocked_stage"] == "consumption"
    assert experience["stage_control"]["completed_prefix"] == [
        "activation", "availability",
    ]
    pool = ExperiencePool(tmp_path / "experience_pool")
    episode_id = pool.append(
        experience,
        trajectory=framework.store.load_observation().to_dict(),
    )
    optimizer_view = pool.optimizer_view()
    assert optimizer_view["episodes"][0]["metrics"]["harness_version"] == 7
    assert optimizer_view["episodes"][0]["stage_control"] == (
        experience["stage_control"]
    )
    assert optimizer_view["episodes"][0]["trajectory_file"] == (
        f"trajectories/{episode_id}.json"
    )
    assert pool.load_trajectory(episode_id) == (
        framework.store.load_observation().to_dict()
    )
    assert json.loads(
        (state / "agent_view/trajectory_state.json").read_text()
    ) == pool.load_trajectory(episode_id)
    serialized = json.dumps(optimizer_view)
    # Full semantics live in the referenced canonical trajectory rather than
    # being duplicated into the compact cross-sample index.
    assert "revised candidate" not in serialized.lower()
    assert "revised candidate" in json.dumps(pool.load_trajectory(episode_id)).lower()


def test_repeated_reminders_without_submission_are_materialization_failure(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse_input(void) { return 0; }\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="no-candidate",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    for _ in range(2):
        framework.record_event(
            source="controller", kind="observer_submission_deferred",
            payload={"reason": "candidate artifacts are not ready"},
        )
    metrics = collect_episode_metrics(framework.store, harness_version=1)
    assert metrics.supervisor_reminders == 2
    assert metrics.total_submissions == 0
    assert metrics.reminder_triggered_submissions == 0
    assert metrics.post_reminder_no_submission is True
    fallback = EpisodeAnalyzer._fallback(metrics, framework.store)
    assert fallback["assessment"] == "harness_limited"
    assert fallback["experiences"][0]["category"] == (
        "candidate_materialization_failure"
    )
    assert fallback["experiences"][0]["confidence"] == "high"
    analyzed = EpisodeAnalyzer(FakeBackend()).analyze(
        store=framework.store, harness_version=1
    )
    assert analyzed["stage_control"]["first_blocked_stage"] == "activation"
    assert analyzed["stage_control"]["stages"]["availability"]["status"] == (
        "not_reached"
    )
    assert analyzed["assessment"] == "mixed"
    assert analyzed["experiences"][0] == {
        "kind": "harness_failure",
        "category": "candidate_materialization_failure",
        "confidence": "high",
        "evidence_sequences": [2, 3],
    }


def test_one_deduplicated_reminder_is_high_confidence_materialization_failure(tmp_path):
    """Reminder deduplication must not hide a failed Reward activation path."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text("int parse_input(void) { return 0; }\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="deduplicated-reminder",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    framework.record_event(
        source="controller", kind="observer_submission_deferred",
        payload={"reason": "candidate artifacts are not ready"},
    )
    for _ in range(3):
        framework.record_event(
            source="controller", kind="observer_materialization_still_pending",
            payload={},
        )

    analyzed = EpisodeAnalyzer(FakeBackend()).analyze(
        store=framework.store, harness_version=1
    )
    materialization = next(
        item for item in analyzed["experiences"]
        if item["category"] == "candidate_materialization_failure"
    )
    assert materialization["kind"] == "harness_failure"
    assert materialization["confidence"] == "high"


def test_pending_submission_surviving_condensation_is_context_loss(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse_input(void) { return 0; }\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="condensed-candidate",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    framework.record_event(
        source="controller", kind="observer_submission_deferred",
        payload={"reason": "candidate artifacts are not ready"},
    )
    framework.record_event(
        source="agent", kind="ActionType.CONDENSATION", payload={},
    )
    framework.record_event(
        source="agent", kind="ActionType.RUN", payload={"action": "run"},
    )
    metrics = collect_episode_metrics(framework.store, harness_version=1)
    assert metrics.condensation_events == 1
    assert metrics.pending_submission_condensations == 1
    assert metrics.post_condensation_no_submission is True
    fallback = EpisodeAnalyzer._fallback(metrics, framework.store)
    assert fallback["experiences"][0]["category"] == "submission_context_loss"
    assert fallback["experiences"][0]["evidence_sequences"] == [2, 3]


def test_preserved_submission_state_is_not_mislabeled_context_loss(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse_input(void) { return 0; }\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="preserved-condensed-candidate",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    framework.record_event(
        source="controller", kind="observer_submission_deferred",
        payload={"reason": "candidate artifacts are not ready"},
    )
    framework.record_event(
        source="agent", kind="ActionType.CONDENSATION",
        payload={
            "message": (
                "SUBMISSION_STATE: materialize both artifacts and call "
                "submit_candidate."
            )
        },
    )
    metrics = collect_episode_metrics(framework.store, harness_version=2)
    assert metrics.pending_submission_condensations == 1
    assert metrics.post_condensation_no_submission is False
    fallback = EpisodeAnalyzer._fallback(metrics, framework.store)
    assert all(
        item["category"] != "submission_context_loss"
        for item in fallback["experiences"]
    )


def test_finalization_preserves_stuck_loop_as_premature_finish(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse_input(void) { return 0; }\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="stuck-loop-finalized",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    framework.record_event(
        source="controller", kind="controller_iteration",
        payload={"iteration": 67, "maximum": 100},
    )
    finalization = framework.record_event(
        source="user", kind="ActionType.MESSAGE",
        payload={
            "message": (
                "[Fine Trace Finalization] The PoC task has ended because: "
                "agent_error: AgentStuckInLoopError: Agent got stuck in a loop."
            )
        },
    )
    metrics = collect_episode_metrics(framework.store, harness_version=3)
    assert metrics.terminal_reason == "agent_stuck_loop"
    normalized = EpisodeAnalyzer._enforce_deterministic_categories(
        {"assessment": "unassessable", "experiences": []},
        metrics,
        framework.store,
    )
    assert normalized["assessment"] == "harness_limited"
    assert normalized["experiences"][0] == {
        "kind": "harness_failure",
        "category": "premature_finish",
        "confidence": "high",
        "evidence_sequences": [2, finalization.events[-1].sequence],
    }


def test_materialization_supervisor_continues_semantic_monitoring(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse_input(void) { return 0; }\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    injected = []
    ready = {"value": False}
    backend = FakeBackend()
    framework = RewardFramework.create(
        task_id="materialization-state",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=backend,
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(
            workspace_root=workspace,
            inject=injected.append,
            submission_ready_callback=lambda: ready["value"],
        ),
    )

    # The task prompt alone cannot trigger the semantic observer.
    framework.record_event(
        source="user", kind="ActionType.MESSAGE", payload={"message": "task"}
    )
    assert framework.observe_trajectory() == "continue"
    assert backend.roles.count("observe_trajectory") == 0

    # One substantive Subject action permits one semantic materialization cue.
    framework.record_event(
        source="agent", kind="ActionType.RUN", payload={"message": "inspect source"}
    )
    assert framework.observe_trajectory() == "continue"
    assert backend.roles.count("observe_trajectory") == 1
    assert framework.status()["materialization_outstanding"] is True
    assert len(injected) == 1

    # Continued work is re-evaluated semantically, but an identical pending
    # obligation is not injected again and does not interrupt the Subject.
    framework.record_event(
        source="agent", kind="ActionType.RUN", payload={"message": "inspect more"}
    )
    assert framework.observe_trajectory() == "continue"
    assert backend.roles.count("observe_trajectory") == 2
    assert len(injected) == 1
    state = framework.store.load_observation()
    assert sum(
        event.kind == "observer_materialization_still_pending"
        for event in state.events
    ) == 1

    # Artifact readiness promotes the existing cue to the hard submit boundary.
    ready["value"] = True
    framework.record_event(
        source="agent", kind="ActionType.RUN", payload={"message": "write artifacts"}
    )
    assert framework.observe_trajectory() == "request_submission"
    assert framework.status()["submission_requested"] is True
    assert len(injected) == 2


def test_repeated_malformed_tool_calls_are_harness_recovery_failure(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text("int parse_input(void) { return 0; }\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="tool-recovery",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    for message in (
        "Missing required parameters for function 'execute_bash': {'command'}",
        "Parameter 'command\"' is not allowed for function 'execute_bash'.",
    ):
        framework.record_event(
            source="agent", kind="ObservationType.ERROR", payload={"message": message}
        )
    framework.record_event(
        source="agent",
        kind="ActionType.MESSAGE",
        payload={
            "message": (
                "<｜DSML｜=execute_bash> <function=execute_bash> "
                "<parameter=command>inspect source "
                "<\" /workspace/private-sample/source.c | head"
            )
        },
    )
    framework.record_event(
        source="agent",
        kind="ActionType.MESSAGE",
        payload={
            "message": (
                "<result><name>execute_bash</name>"
                "<command>inspect another source</command></result>"
            )
        },
    )
    metrics = collect_episode_metrics(framework.store, harness_version=1)
    assert metrics.tool_protocol_errors == 4
    assert metrics.invalid_tool_parameter_errors == 2
    assert metrics.unparsed_tool_intents == 2
    evidence = collect_protocol_evidence(
        framework.store.load_observation().to_dict()["events"]
    )
    assert evidence["invalid_tool_parameter_events"] == 2
    assert evidence["invalid_tool_parameter_signatures"] == {
        "missing_required_parameters": 1,
        "unknown_parameter": 1,
        "invalid_tool_call": 0,
    }
    assert sorted(evidence["unparsed_tool_tag_shapes"]) == sorted([
        [
            "<｜DSML｜=execute_bash>",
            "<function=execute_bash>",
            "<parameter=command>",
        ],
        [
            "<result>",
            "<name>",
            "</name>",
            "<command>",
            "</command>",
            "</result>",
        ],
    ])
    assert "inspect source" not in json.dumps(evidence)
    assert "inspect another source" not in json.dumps(evidence)
    assert "private-sample" not in json.dumps(evidence)
    analyzed = EpisodeAnalyzer(FakeBackend()).analyze(
        store=framework.store, harness_version=1
    )
    assert analyzed["assessment"] in {"harness_limited", "mixed"}
    assert analyzed["experiences"][0]["category"] == (
        "tool_protocol_recovery_failure"
    )


def test_missing_tool_arguments_do_not_authorize_harness_to_invent_intent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text("int parse_input(void) { return 0; }\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="ambiguous-tool-call",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    for _ in range(3):
        framework.record_event(
            source="agent", kind="ObservationType.ERROR",
            payload={
                "message": (
                    "Missing required parameters for function "
                    "'execute_bash': {'command'}"
                )
            },
        )
    analyzed = EpisodeAnalyzer(FakeBackend()).analyze(
        store=framework.store, harness_version=1
    )
    protocol = next(
        item for item in analyzed["experiences"]
        if item["category"] == "tool_protocol_recovery_failure"
    )
    assert protocol["kind"] == "subject_failure"


def test_iteration_limit_without_candidate_is_missing_submission_not_instrumentation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text("int parse_input(void) { return 0; }\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="no-submit-limit",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), False
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    framework.reach_iteration_limit(iteration=100, maximum=100)
    analyzed = EpisodeAnalyzer(FakeBackend()).analyze(
        store=framework.store, harness_version=1
    )
    categories = {item["category"] for item in analyzed["experiences"]}
    assert "missing_submission" in categories
    assert "instrumentation_unavailable" not in categories


def test_confirmed_trigger_cannot_be_labeled_causal_stagnation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text("int parse_input(void) { return 0; }\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="successful-candidate",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    state = framework.record_event(
        source="controller", kind="verification_completed", payload={}
    )
    event = state.events[-1]
    metrics = replace(
        collect_episode_metrics(framework.store, harness_version=1),
        trigger_success=True,
    )
    normalized = EpisodeAnalyzer._enforce_deterministic_categories(
        {
            "assessment": "mixed",
            "experiences": [
                {
                    "kind": "success_signal",
                    "category": "trigger_success",
                    "confidence": "high",
                    "evidence_sequences": [event.sequence],
                },
                {
                    "kind": "subject_failure",
                    "category": "causal_stagnation",
                    "confidence": "medium",
                    "evidence_sequences": [event.sequence],
                },
            ],
        },
        metrics,
        framework.store,
    )
    assert normalized["assessment"] == "successful"
    assert [item["category"] for item in normalized["experiences"]] == [
        "trigger_success"
    ]


def test_success_after_distinct_retry_completes_consumption_and_progress(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text("int parse_input(void) { return 0; }\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="retry-success-control",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), True
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    for attempt, candidate, trigger, root_status in (
        (1, "first", False, "not_reached"),
        (2, "second", True, "confirmed"),
    ):
        framework.record_event(
            source="agent", kind="candidate_submitted",
            payload={"attempt_number": attempt, "duplicate_of": None},
        )
        framework.record_event(
            source="reward_agent", kind="verification_completed",
            payload={"attempt_number": attempt},
        )
    framework.store.save_evidence_state(EvidenceState(attempts=[
        {
            "candidate_id": "first", "duplicate_of": None,
            "trigger_observed": False, "instrumentation_available": True,
            "assessment": {"stages": {"root": "not_reached"}},
        },
        {
            "candidate_id": "second", "duplicate_of": None,
            "trigger_observed": True, "instrumentation_available": True,
            "assessment": {"stages": {"root": root_status}},
        },
    ]))
    metrics = collect_episode_metrics(framework.store, harness_version=1)
    control = build_stage_control(framework.store, metrics)
    assert metrics.trigger_success is True
    assert metrics.distinct_retries_after_reward == 1
    assert control["stages"]["consumption"]["status"] == "complete"
    assert control["stages"]["progress"]["status"] == "complete"
    assert control["stages"]["success"]["status"] == "complete"
    assert control["first_blocked_stage"] is None


def test_impossible_success_and_duplicate_categories_are_removed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text("int parse_input(void) { return 0; }\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="impossible-categories",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), False
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    state = framework.record_event(
        source="controller", kind="controller_iteration",
        payload={"iteration": 1, "maximum": 100},
    )
    sequence = state.events[-1].sequence
    metrics = collect_episode_metrics(framework.store, harness_version=1)
    normalized = EpisodeAnalyzer._enforce_deterministic_categories(
        {
            "assessment": "successful",
            "experiences": [
                {"kind": "success_signal", "category": "trigger_success",
                 "confidence": "high", "evidence_sequences": [sequence]},
                {"kind": "success_signal", "category": "causal_progress",
                 "confidence": "high", "evidence_sequences": [sequence]},
                {"kind": "success_signal", "category": "productive_retry",
                 "confidence": "high", "evidence_sequences": [sequence]},
                {"kind": "harness_failure", "category": "duplicate_candidate_loop",
                 "confidence": "high", "evidence_sequences": [sequence]},
            ],
        },
        metrics,
        framework.store,
    )
    assert normalized["experiences"] == []


def test_duplicate_before_subject_can_consume_reward_is_harness_owned(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text("int parse_input(void) { return 0; }\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    framework = RewardFramework.create(
        task_id="duplicate-race",
        issue_description="A parser operation reaches an unsafe state.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), False
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=lambda _: None),
    )
    framework.record_event(
        source="coding_agent", kind="candidate_submitted",
        payload={"candidate_id": "one", "duplicate_of": None},
    )
    framework.record_event(
        source="reward_agent", kind="verification_completed",
        payload={"candidate_id": "one"},
    )
    framework.record_event(
        source="coding_agent", kind="candidate_submitted",
        payload={"candidate_id": "one", "duplicate_of": "one"},
    )
    framework.store.save_evidence_state(EvidenceState(attempts=[
        {
            "candidate_id": "one", "duplicate_of": None,
            "instrumentation_available": True, "assessment": {},
        },
        {
            "candidate_id": "one", "duplicate_of": "one",
            "instrumentation_available": True, "assessment": {},
        },
    ]))
    metrics = collect_episode_metrics(framework.store, harness_version=1)
    assert metrics.duplicate_submissions_without_feedback_action == 1
    normalized = EpisodeAnalyzer._enforce_deterministic_categories(
        {"assessment": "subject_limited", "experiences": []},
        metrics,
        framework.store,
    )
    duplicate = next(
        item for item in normalized["experiences"]
        if item["category"] == "duplicate_candidate_loop"
    )
    assert duplicate["kind"] == "harness_failure"


def test_cross_sample_patcher_edits_real_fork_for_next_episode(tmp_path):
    pristine = tmp_path / "pristine"
    controller = pristine / "openhands/controller"
    controller.mkdir(parents=True)
    core_file = controller / "agent_controller.py"
    core_file.write_text("class AgentController: pass\n", encoding="utf-8")
    repository = HarnessRepository(tmp_path / "training/harness", pristine)
    assert repository.initialize() == 1
    pool = ExperiencePool(tmp_path / "training/experience_pool")
    episode_id = pool.append({
        "created_at": "now",
        "metrics": {
            "harness_version": 1, "terminal_reason": "iteration_limit",
            "trigger_success": False, "trajectory_events": 100,
            "total_submissions": 0, "unique_candidates": 0,
            "duplicate_submissions": 0, "duplicate_ratio": 0.0,
            "invalid_submissions": 0,
            "episodes_with_submission": False,
            "first_submission_sequence": None, "first_submission_iteration": None,
            "submission_requests": 0,
            "reward_events": 0, "rewards_followed_by_subject_action": 0,
            "distinct_retries_after_reward": 0, "causal_progress_events": 0,
            "instrumentation_unavailable_attempts": 0,
            "early_finish_rejections": 0,
        },
        "assessment": "harness_limited",
        "experiences": [{
            "kind": "harness_failure", "category": "missing_submission",
            "confidence": "high", "evidence_sequences": [100],
        }],
    }, trajectory={
        "events": [{
            "sequence": 100,
            "timestamp": "now",
            "source": "agent",
            "kind": "ActionType.MESSAGE",
            "payload": {"message": "complete training trajectory"},
        }],
        "last_observer_sequence": 0,
        "last_submission_sequence": 0,
        "submission_requested": False,
        "materialization_outstanding": False,
        "awaiting_verification": False,
        "terminal_reason": "iteration_limit",
    })

    class RealSourcePatcher:
        model = "fake-patcher"

        def run_json(self, *, role, prompt, schema, cwd):
            view = json.loads(
                (Path(cwd) / ".harness_optimizer/experience_pool.json").read_text()
            )
            assert view["episodes"][0]["trajectory_file"] == (
                f"trajectories/{episode_id}.json"
            )
            trajectory = json.loads(
                (Path(cwd) / ".harness_optimizer/trajectories"
                 / f"{episode_id}.json").read_text()
            )
            assert trajectory["events"][0]["payload"]["message"] == (
                "complete training trajectory"
            )
            path = Path(cwd) / "openhands/controller/agent_controller.py"
            assert "AgentController" in path.read_text()
            path.write_text(
                "SUBMISSION_OBSERVATION_ENABLED = True\n"
                "class AgentController: pass\n", encoding="utf-8",
            )
            return {
                "decision": "patch",
                "failure_categories": ["missing_submission"],
                "changed_files": ["openhands/controller/agent_controller.py"],
            }

    revision = CrossSampleHarnessPatcher(RealSourcePatcher()).update(
        pool=pool, repository=repository
    )
    assert revision["version"] == 2
    assert repository.active_version == 2
    assert "SUBMISSION_OBSERVATION_ENABLED" in (
        repository.worktree / "openhands/controller/agent_controller.py"
    ).read_text()
    assert core_file.read_text() == "class AgentController: pass\n"
    assert (repository.versions / "v0002/patch.diff").is_file()


def test_cross_sample_patcher_cannot_modify_canonical_trajectory_view(tmp_path):
    pristine = tmp_path / "pristine"
    controller = pristine / "openhands/controller"
    controller.mkdir(parents=True)
    (controller / "agent_controller.py").write_text(
        "class AgentController: pass\n", encoding="utf-8"
    )
    repository = HarnessRepository(tmp_path / "training/harness", pristine)
    repository.initialize()
    pool = ExperiencePool(tmp_path / "training/experience_pool")
    episode_id = pool.append({
        "created_at": "now",
        "metrics": {
            "harness_version": 1, "terminal_reason": "iteration_limit",
            "trigger_success": False, "trajectory_events": 1,
            "total_submissions": 0, "unique_candidates": 0,
            "duplicate_submissions": 0, "duplicate_ratio": 0.0,
            "invalid_submissions": 0, "episodes_with_submission": False,
            "first_submission_sequence": None, "first_submission_iteration": None,
            "submission_requests": 0, "reward_events": 0,
            "rewards_followed_by_subject_action": 0,
            "distinct_retries_after_reward": 0, "causal_progress_events": 0,
            "instrumentation_unavailable_attempts": 0,
            "early_finish_rejections": 0,
        },
        "assessment": "harness_limited",
        "experiences": [{
            "kind": "harness_failure", "category": "missing_submission",
            "confidence": "high", "evidence_sequences": [1],
        }],
    }, trajectory={"events": [{
        "sequence": 1, "timestamp": "now", "source": "agent",
        "kind": "ActionType.MESSAGE", "payload": {"message": "trajectory"},
    }]})

    class MutatingInputsPatcher:
        model = "fake-patcher"

        def reset_session(self):
            pass

        def run_json(self, *, role, prompt, schema, cwd):
            trajectory = (
                Path(cwd) / ".harness_optimizer/trajectories"
                / f"{episode_id}.json"
            )
            trajectory.write_text('{"events": []}\n', encoding="utf-8")
            target = Path(cwd) / "openhands/controller/agent_controller.py"
            target.write_text(
                "SUBMISSION_STATE = True\nclass AgentController: pass\n",
                encoding="utf-8",
            )
            return {
                "decision": "patch",
                "failure_categories": ["missing_submission"],
                "changed_files": ["openhands/controller/agent_controller.py"],
            }

    try:
        CrossSampleHarnessPatcher(MutatingInputsPatcher()).update(
            pool=pool, repository=repository
        )
    except ValueError as exc:
        assert "trajectory/experience inputs" in str(exc)
    else:
        raise AssertionError("Patcher modified its canonical trajectory input")
    assert repository.active_version == 1


def test_harness_runtime_contract_accepts_evolved_message_state_and_rejects_abi_break(
    tmp_path,
):
    pristine = tmp_path / "pristine"
    controller = pristine / "openhands/controller"
    agent_dir = pristine / "openhands/agenthub/codeact_agent"
    controller.mkdir(parents=True)
    agent_dir.mkdir(parents=True)
    controller_file = controller / "agent_controller.py"
    controller_file.write_text(
        "class AgentController:\n"
        "    async def _handle_action(self, action): pass\n"
        "    async def _step(self): pass\n"
        "    async def set_agent_state_to(self, state): pass\n"
        "    def _is_stuck(self): return False\n",
        encoding="utf-8",
    )
    agent_file = agent_dir / "codeact_agent.py"
    agent_file.write_text(
        "class CodeActAgent:\n"
        "    def step(self, state): pass\n"
        "    def _get_messages(self, events, state): return []\n",
        encoding="utf-8",
    )
    repository = HarnessRepository(tmp_path / "training/harness", pristine)
    repository.initialize()
    repository.validate_runtime_contracts()

    worktree_agent = (
        repository.worktree
        / "openhands/agenthub/codeact_agent/codeact_agent.py"
    )
    worktree_agent.write_text(
        "class CodeActAgent:\n"
        "    def step(self): pass\n"
        "    def _get_messages(self, events, state): return []\n",
        encoding="utf-8",
    )
    try:
        repository.validate_runtime_contracts()
    except ValueError as exc:
        assert "CodeActAgent.step" in str(exc)
    else:
        raise AssertionError("an incompatible Patcher ABI change was accepted")


def test_cross_sample_patcher_sees_recurrent_claimed_failure(tmp_path):
    pristine = tmp_path / "pristine"
    controller = pristine / "openhands/controller"
    controller.mkdir(parents=True)
    (controller / "agent_controller.py").write_text(
        "class AgentController: pass\n", encoding="utf-8"
    )
    repository = HarnessRepository(tmp_path / "training/harness", pristine)
    repository.initialize()
    active = json.loads(repository.active_path.read_text(encoding="utf-8"))
    active.update({
        "version": 2,
        "parent": 1,
        "failure_categories": ["candidate_materialization_failure"],
    })
    repository.active_path.write_text(json.dumps(active), encoding="utf-8")

    pool = ExperiencePool(tmp_path / "training/experience_pool")
    pool.append({
        "created_at": "now",
        "metrics": {
            "harness_version": 2, "terminal_reason": "iteration_limit",
            "trigger_success": False, "trajectory_events": 100,
            "total_submissions": 0, "unique_candidates": 0,
            "duplicate_submissions": 0, "duplicate_ratio": 0.0,
            "invalid_submissions": 0, "episodes_with_submission": False,
            "first_submission_sequence": None, "first_submission_iteration": None,
            "submission_requests": 0, "reward_events": 0,
            "rewards_followed_by_subject_action": 0,
            "distinct_retries_after_reward": 0, "causal_progress_events": 0,
            "instrumentation_unavailable_attempts": 0,
            "early_finish_rejections": 0,
        },
        "assessment": "harness_limited",
        "experiences": [{
            "kind": "harness_failure",
            "category": "candidate_materialization_failure",
            "confidence": "high", "evidence_sequences": [100],
        }],
    })

    class InspectingPatcher:
        model = "fake-patcher"

        def __init__(self):
            self.calls = 0
            self.resets = 0

        def reset_session(self):
            self.resets += 1

        def run_json(self, *, role, prompt, schema, cwd):
            self.calls += 1
            view = json.loads(
                (Path(cwd) / ".harness_optimizer/experience_pool.json").read_text()
            )
            effectiveness = view["active_patch_effectiveness"]
            assert effectiveness["active_version"] == 2
            assert effectiveness["interpretation"] == "empirically_ineffective"
            assert effectiveness["empirically_recurrent"] == {
                "candidate_materialization_failure": 1
            }
            assert effectiveness["actionable_controller_failures"] == [
                "candidate_materialization_failure"
            ]
            if self.calls >= 2:
                assert "previous proposal failed" in prompt
            return {"decision": "keep", "failure_categories": [], "changed_files": []}

    backend = InspectingPatcher()
    try:
        CrossSampleHarnessPatcher(backend).update(pool=pool, repository=repository)
    except ValueError as exc:
        assert "unresolved high-confidence controller" in str(exc)
    else:
        raise AssertionError("actionable controller failure was silently kept")
    assert backend.calls == 3
    assert backend.resets == 3


def test_iteration_limit_without_submission_is_actionable_for_patcher(tmp_path):
    pristine = tmp_path / "pristine"
    controller = pristine / "openhands/controller"
    controller.mkdir(parents=True)
    (controller / "agent_controller.py").write_text(
        "class AgentController: pass\n", encoding="utf-8"
    )
    repository = HarnessRepository(tmp_path / "training/harness", pristine)
    repository.initialize()
    pool = ExperiencePool(tmp_path / "training/experience_pool")
    pool.append({
        "created_at": "now",
        "metrics": {
            "harness_version": 1, "terminal_reason": "iteration_limit",
            "trigger_success": False, "trajectory_events": 100,
            "total_submissions": 0, "unique_candidates": 0,
            "duplicate_submissions": 0, "duplicate_ratio": 0.0,
            "invalid_submissions": 0, "episodes_with_submission": False,
            "first_submission_sequence": None, "first_submission_iteration": None,
            "submission_requests": 0, "reward_events": 0,
            "rewards_followed_by_subject_action": 0,
            "distinct_retries_after_reward": 0, "causal_progress_events": 0,
            "instrumentation_unavailable_attempts": 0,
            "early_finish_rejections": 0,
        },
        "assessment": "harness_limited",
        "experiences": [{
            "kind": "harness_failure", "category": "missing_submission",
            "confidence": "high", "evidence_sequences": [100],
        }],
    })

    class InspectingPatcher:
        model = "fake-patcher"

        def reset_session(self):
            pass

        def run_json(self, *, role, prompt, schema, cwd):
            view = json.loads(
                (Path(cwd) / ".harness_optimizer/experience_pool.json").read_text()
            )
            effectiveness = view["active_patch_effectiveness"]
            assert effectiveness["actionable_controller_failures"] == [
                "missing_submission"
            ]
            assert effectiveness["reward_activation_blockers"] == [
                "missing_submission"
            ]
            return {"decision": "keep", "failure_categories": [], "changed_files": []}

    try:
        CrossSampleHarnessPatcher(InspectingPatcher()).update(
            pool=pool, repository=repository
        )
    except ValueError as exc:
        assert "unresolved high-confidence controller" in str(exc)
    else:
        raise AssertionError("zero-submission iteration limit was silently kept")


def test_cross_sample_rejects_unrelated_prompt_example_patch(tmp_path):
    pristine = tmp_path / "pristine"
    prompt_dir = pristine / "openhands/agenthub/codeact_agent/prompts"
    prompt_dir.mkdir(parents=True)
    prompt = prompt_dir / "in_context_learning_example.j2"
    original = "<function=finish>\n</function>\n"
    prompt.write_text(original, encoding="utf-8")
    repository = HarnessRepository(tmp_path / "training/harness", pristine)
    repository.initialize()
    pool = ExperiencePool(tmp_path / "training/experience_pool")

    class SuperficialPatcher:
        model = "fake-patcher"

        def run_json(self, *, role, prompt, schema, cwd):
            target = Path(cwd) / (
                "openhands/agenthub/codeact_agent/prompts/"
                "in_context_learning_example.j2"
            )
            target.write_text(
                "<function=finish>\n"
                "<parameter=message>done</parameter>\n"
                "<parameter=task_completed>true</parameter>\n"
                "</function>\n",
                encoding="utf-8",
            )
            return {
                "decision": "patch",
                "failure_categories": [
                    "tool_protocol_recovery_failure",
                    "candidate_materialization_failure",
                ],
                "changed_files": [
                    "openhands/agenthub/codeact_agent/prompts/"
                    "in_context_learning_example.j2"
                ],
            }

    try:
        CrossSampleHarnessPatcher(SuperficialPatcher()).update(
            pool=pool, repository=repository
        )
    except ValueError as exc:
        assert "tool_protocol_recovery_failure claim" in str(exc)
    else:
        raise AssertionError("unrelated prompt-example patch was accepted")
    assert repository.active_version == 1
    assert (
        repository.worktree
        / "openhands/agenthub/codeact_agent/prompts/in_context_learning_example.j2"
    ).read_text() == original


def test_submission_protocol_claim_requires_submit_routing_change(tmp_path):
    pristine = tmp_path / "pristine"
    agent_dir = pristine / "openhands/agenthub/codeact_agent"
    agent_dir.mkdir(parents=True)
    target = agent_dir / "function_calling.py"
    target.write_text(
        "def parse_tool(value):\n    return value\n", encoding="utf-8"
    )
    repository = HarnessRepository(tmp_path / "training/harness", pristine)
    repository.initialize()
    before = repository.snapshot()
    worktree_target = (
        repository.worktree
        / "openhands/agenthub/codeact_agent/function_calling.py"
    )
    worktree_target.write_text(
        "def parse_tool(value):\n    return value.strip()\n", encoding="utf-8"
    )
    after = repository.snapshot()
    changed = repository.validate_changes(before, after)
    try:
        repository.validate_failure_alignment(
            before=before,
            changed=changed,
            categories=["invalid_submission_protocol"],
        )
    except ValueError as exc:
        assert "submit_candidate parsing or routing" in str(exc)
    else:
        raise AssertionError("unrelated tool parsing claimed a submission repair")


def test_protocol_evidence_prioritizes_late_first_class_submission_shape():
    events = []
    for index in range(10):
        events.append({
            "sequence": index + 1,
            "source": "agent",
            "kind": "ActionType.MESSAGE",
            "payload": {
                "message": (
                    f"<｜｜DSML｜｜=execute_bash><parameter=command>"
                    f"ignored-{index}</｜｜DSML｜｜>"
                )
            },
        })
    events.append({
        "sequence": 20,
        "source": "agent",
        "kind": "ActionType.MESSAGE",
        "payload": {
            "message": (
                "<｜｜DSML｜｜=submit_candidate>"
                "<parameter=poc_path>/secret/value</parameter>"
                "<parameter=trace_path>/secret/trace</parameter>"
                "</｜｜DSML｜｜>"
            )
        },
    })
    evidence = collect_protocol_evidence(events)
    shapes = evidence["unparsed_tool_tag_shapes"]
    assert any(any("submit_candidate" in tag for tag in shape) for shape in shapes)
    serialized = json.dumps(evidence)
    assert "/secret/value" not in serialized
    assert "/secret/trace" not in serialized


def test_cross_sample_patch_with_dataset_literal_rolls_back(tmp_path):
    pristine = tmp_path / "pristine"
    controller = pristine / "openhands/controller"
    controller.mkdir(parents=True)
    original = "class AgentController: pass\n"
    (controller / "agent_controller.py").write_text(original, encoding="utf-8")
    repository = HarnessRepository(tmp_path / "training/harness", pristine)
    repository.initialize()
    pool = ExperiencePool(tmp_path / "training/experience_pool")
    pool.append({
        "created_at": "now", "metrics": {
            "harness_version": 1, "terminal_reason": "iteration_limit",
            "trigger_success": False, "trajectory_events": 1,
            "total_submissions": 0, "unique_candidates": 0,
            "duplicate_submissions": 0, "duplicate_ratio": 0.0,
            "invalid_submissions": 0, "first_submission_sequence": None,
            "episodes_with_submission": False, "first_submission_iteration": None,
            "submission_requests": 0, "reward_events": 0,
            "rewards_followed_by_subject_action": 0,
            "distinct_retries_after_reward": 0, "causal_progress_events": 0,
            "instrumentation_unavailable_attempts": 0,
            "early_finish_rejections": 0,
        }, "assessment": "harness_limited", "experiences": [{
            "kind": "harness_failure", "category": "missing_submission",
            "confidence": "medium", "evidence_sequences": [1],
        }],
    })

    class LeakingPatcher:
        model = "fake-patcher"

        def run_json(self, *, role, prompt, schema, cwd):
            path = Path(cwd) / "openhands/controller/agent_controller.py"
            path.write_text('SPECIAL_CASE = "arvo_1234"\n' + original)
            return {
                "decision": "patch", "failure_categories": ["missing_submission"],
                "changed_files": ["openhands/controller/agent_controller.py"],
            }

    try:
        CrossSampleHarnessPatcher(LeakingPatcher()).update(
            pool=pool, repository=repository
        )
    except ValueError as exc:
        assert "sample-specific literal" in str(exc)
    else:
        raise AssertionError("dataset-specific harness patch was accepted")
    assert repository.active_version == 1
    assert (
        repository.worktree / "openhands/controller/agent_controller.py"
    ).read_text() == original


def test_explicit_harness_profiles_isolate_pristine_evaluation(monkeypatch):
    monkeypatch.setenv("OPENHANDS_REWARD_FRAMEWORK", "1")
    monkeypatch.setenv("REWARD_FRAMEWORK_CROSS_SAMPLE_TRAINING", "1")
    configure_harness_profile("baseline", max_iterations=100)
    assert os.environ["OPENHANDS_MAIN_MODULE"] == "openhands.core.main"
    assert "OPENHANDS_REWARD_FRAMEWORK" not in os.environ
    assert "REWARD_FRAMEWORK_CROSS_SAMPLE_TRAINING" not in os.environ

    configure_harness_profile("reward", max_iterations=100)
    assert os.environ["OPENHANDS_MAIN_MODULE"] == "reward_framework.openhands_entrypoint"
    assert os.environ["REWARD_FRAMEWORK_CROSS_SAMPLE_TRAINING"] == "1"

    monkeypatch.setenv("REWARD_FRAMEWORK_EPISODE_OPENHANDS_ROOT", "/frozen/fork")
    configure_harness_profile("reward", max_iterations=100, update_harness=False)
    assert os.environ["OPENHANDS_MAIN_MODULE"] == "reward_framework.openhands_entrypoint"
    assert os.environ["OPENHANDS_REQUIRE_PRISTINE"] == "0"
    assert "REWARD_FRAMEWORK_CROSS_SAMPLE_TRAINING" not in os.environ
    assert os.environ["REWARD_FRAMEWORK_EPISODE_OPENHANDS_ROOT"] == "/frozen/fork"


def test_reward_subject_llm_recovery_is_finite_and_configurable(monkeypatch):
    for name in (
        "REWARD_FRAMEWORK_SUBJECT_LLM_TIMEOUT",
        "REWARD_FRAMEWORK_SUBJECT_LLM_ATTEMPTS",
        "REWARD_FRAMEWORK_SUBJECT_LLM_RETRY_MIN_WAIT",
        "REWARD_FRAMEWORK_SUBJECT_LLM_RETRY_MAX_WAIT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert reward_subject_llm_recovery_config() == {
        "timeout": 90,
        "num_retries": 3,
        "retry_multiplier": 2.0,
        "retry_min_wait": 2,
        "retry_max_wait": 15,
    }

    monkeypatch.setenv("REWARD_FRAMEWORK_SUBJECT_LLM_TIMEOUT", "30")
    monkeypatch.setenv("REWARD_FRAMEWORK_SUBJECT_LLM_ATTEMPTS", "2")
    monkeypatch.setenv("REWARD_FRAMEWORK_SUBJECT_LLM_RETRY_MIN_WAIT", "3")
    monkeypatch.setenv("REWARD_FRAMEWORK_SUBJECT_LLM_RETRY_MAX_WAIT", "8")
    configured = reward_subject_llm_recovery_config()
    assert configured["timeout"] == 30
    assert configured["num_retries"] == 2
    assert configured["retry_min_wait"] == 3
    assert configured["retry_max_wait"] == 8

    monkeypatch.setenv("REWARD_FRAMEWORK_SUBJECT_LLM_RETRY_MAX_WAIT", "1")
    try:
        reward_subject_llm_recovery_config()
    except ValueError as exc:
        assert "MAX_WAIT" in str(exc)
    else:
        raise AssertionError("an invalid retry interval was accepted")


def test_subject_wall_clock_watchdog_interrupts_blocked_provider_call():
    import signal

    def blocked():
        # Trigger the handler installed by _call_with_wall_timeout immediately;
        # this tests its control flow without adding a one-second sleep.
        signal.raise_signal(signal.SIGALRM)

    try:
        _call_with_wall_timeout(blocked, 60)
    except _SubjectWallClockTimeout:
        pass
    else:
        raise AssertionError("subject provider watchdog did not interrupt the call")


def test_iteration_limit_is_only_non_trigger_terminal(tmp_path):
    # Exercise the terminal transition without constructing another full task.
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse_input(void) { return 0; }\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    injected = []
    framework = RewardFramework.create(
        task_id="limit", issue_description="The parser reaches an unsafe state.",
        codebase_root=source, state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            0, "", "", False, {}, (), False
        )),
        platform=CallbackAdapter(workspace_root=workspace, inject=injected.append),
    )
    assert framework.reach_iteration_limit(iteration=99, maximum=100) is False
    assert framework.reach_iteration_limit(iteration=100, maximum=100) is True
    assert framework.status()["terminal_reason"] == "iteration_limit"
    assert "Stop using tools" in injected[-1]


def test_openhands_iteration_limit_is_not_an_infrastructure_error():
    capped = SimpleNamespace(
        agent_state=AgentState.ERROR,
        last_error="RuntimeError: Agent reached maximum iteration of 100",
    )
    provider_failure = SimpleNamespace(
        agent_state=AgentState.ERROR,
        last_error="LLM provider retries exhausted",
    )
    assert _is_iteration_limit_error(capped) is True
    assert _is_iteration_limit_error(provider_failure) is False


def test_probe_statement_is_resolved_to_current_source_line(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse(int length, int capacity) {\n"
        "  if (length >\n"
        "      capacity) return -1;\n"
        "}\n",
        encoding="utf-8",
    )
    plan = ProbePlan((Probe(
        stage="root", anchor_kind="issue", file="parser.c", function="parse",
        statement="if (length > capacity) return -1;",
        captures=("length", "capacity"), condition="length > capacity",
        purpose="Observe the issue-relevant size relation.",
    ),))
    checkpoint = compile_checkpoints(source, plan)[0]
    assert checkpoint["line"] == 2
    assert checkpoint["captures"]["__reward_condition"] == "length > capacity"


def test_reward_spec_cache_is_keyed_by_public_issue_source_prompt_and_model(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "parser.c").write_text(
        "int parse_input(int length, int capacity) { return length > capacity; }\n",
        encoding="utf-8",
    )
    cache = tmp_path / "spec_cache"
    first_backend = FakeBackend()
    first = SpecAgent(first_backend, cache_root=cache).initialize(
        task_id="cache-1",
        issue_description="An oversized parser length reaches a copy.",
        codebase_root=source,
        agent_root=tmp_path / "agent-1",
    )
    assert first_backend.roles.count("initialize_spec") == 1
    assert len(list(cache.glob("*.json"))) == 1

    second_backend = FakeBackend()
    second = SpecAgent(second_backend, cache_root=cache).initialize(
        task_id="cache-2",
        issue_description="An oversized parser length reaches a copy.",
        codebase_root=source,
        agent_root=tmp_path / "agent-2",
    )
    assert second_backend.roles == []
    assert second.reward_spec.to_dict() == first.reward_spec.to_dict()

    changed_issue_backend = FakeBackend()
    SpecAgent(changed_issue_backend, cache_root=cache).initialize(
        task_id="cache-3",
        issue_description="A different public issue reaches the same copy.",
        codebase_root=source,
        agent_root=tmp_path / "agent-3",
    )
    assert changed_issue_backend.roles.count("initialize_spec") == 1


def test_agent_source_citations_are_canonicalized_only_when_unique(tmp_path):
    source = tmp_path / "source"
    nested = source / "project" / "src"
    nested.mkdir(parents=True)
    (nested / "parser.c").write_text(
        "int parse_input(void) { return 0; }\n", encoding="utf-8"
    )
    assert resolve_public_source_path(
        source, "src/parser.c", "parse_input"
    ) == "project/src/parser.c"
    spec = RewardSpec(
        {
            "admission": "The parser accepts input.",
            "source": None, "root": None, "propagation": None, "target": None,
        },
        {
            "admission": (SourceAnchor(
                "parser.c", "parse_input", "The public parser entry executes."
            ),),
            "source": (), "root": (), "propagation": (), "target": (),
        },
    )
    fixed = canonicalize_spec_sources(spec, source)
    assert fixed.evidence["admission"][0].file == "project/src/parser.c"

    fuzz = source / "project" / "src" / "bin" / "fuzz"
    fuzz.mkdir(parents=True)
    (fuzz / "config_fuzzer.cc").write_text(
        "int LLVMFuzzerTestOneInput(const char*, unsigned long) { return 0; }\n",
        encoding="utf-8",
    )
    (source / "other.cc").write_text(
        "int LLVMFuzzerTestOneInput(const char*, unsigned long);\n",
        encoding="utf-8",
    )
    assert resolve_public_source_path(
        source, "src/bin/config_fuzzer/config_fuzzer.c", "LLVMFuzzerTestOneInput"
    ) == "project/src/bin/fuzz/config_fuzzer.cc"

    prefixed = RewardSpec(
        spec.claims,
        {
            **spec.evidence,
            "admission": (SourceAnchor(
                "parser.c", "project_parse_input", "The parser executes."
            ),),
        },
    )
    repaired = canonicalize_spec_sources(prefixed, source)
    assert repaired.evidence["admission"][0].function == "parse_input"


def test_gdb_observation_requires_condition_truth_for_confirmation():
    checkpoint = {
        "event_point": "reward_01_root_issue",
        "reward_probe": {"stage": "root", "condition": "length > capacity"},
        "file": "parser.c", "function": "parse", "line": 2,
    }
    hit = {
        "event_point": "reward_01_root_issue", "line": 2,
        "file": "parser.c", "function": "parse",
        "fields": {"__reward_condition": 0, "capture_1": 4},
    }
    observations, facts = ArvoGDBInstrumentationBackend._observations(
        [checkpoint], [hit], True
    )
    assert observations["root"] == StageStatus.REFUTED
    assert facts[0].data["condition_value"] is False


def test_openhands_transport_calls_host_framework(tmp_path):
    workspace = tmp_path / "workspace"
    submission = workspace / ".reward_submissions" / ("a" * 32)
    submission.mkdir(parents=True)
    (submission / "poc").write_bytes(b"poc")
    trace = json.dumps([{
        "step": 1, "file": "parser.c", "function": "parse", "line": 1,
        "var": "input", "code": "parse(input)", "note": "Input is parsed.",
    }])
    (submission / "trace.json").write_text(trace, encoding="utf-8")

    class Framework:
        def __init__(self):
            self.calls = []

        def submit_candidate(self, arguments):
            self.calls.append(arguments)
            return {"triggered": False, "attempt_number": 1}

        def status(self):
            return {"terminal_reason": None}

    framework = Framework()
    previous = os.environ.get("OPENHANDS_EVAL_HOST_GATEWAY")
    os.environ["OPENHANDS_EVAL_HOST_GATEWAY"] = "127.0.0.1"
    transport = OpenHandsRewardTransport(
        workspace_root=workspace, framework=framework
    )
    transport.start()
    try:
        request = urllib.request.Request(
            transport.url + "/submit",
            data=json.dumps({
                "token": transport.token, "attempt_id": "a" * 32
            }).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.load(response)
        assert result == {"triggered": False, "attempt_number": 1}
        assert framework.calls[0]["poc_path"].endswith("/poc")
        assert (workspace / ".reward_framework/submit_candidate.py").is_file()
        assert (workspace / ".poc_submission_recorded").is_file()
        assert (workspace / ".latest_candidate_trace.json").read_text() == trace
    finally:
        transport.close()
        if previous is None:
            os.environ.pop("OPENHANDS_EVAL_HOST_GATEWAY", None)
        else:
            os.environ["OPENHANDS_EVAL_HOST_GATEWAY"] = previous


def test_fine_trace_overlay_finds_reward_submission_marker(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = json.dumps([{
        "step": 1, "file": "parser.c", "function": "parse", "line": 1,
        "var": "input", "code": "parse(input)", "note": "Input is parsed.",
    }])
    (workspace / ".poc_submission_recorded").write_text(
        "reward_framework\n", encoding="utf-8"
    )
    (workspace / ".latest_candidate_trace.json").write_text(
        trace, encoding="utf-8"
    )
    monkeypatch.setenv("OPENHANDS_TASK_WORKSPACE", str(workspace))
    monkeypatch.delenv("OPENHANDS_POC_SUBMISSION_MARKER", raising=False)
    monkeypatch.delenv("OPENHANDS_LATEST_SUBMISSION_TRACE", raising=False)
    assert _submitted_trace() == trace


def test_reward_bridge_shell_submission_is_synchronous_boundary():
    reward = SimpleNamespace(
        command=(
            "python3 /workspace/.reward_framework/submit_candidate.py "
            "/workspace/poc.bin /workspace/candidate_trace.json"
        ),
        is_input=False,
    )
    unrelated = SimpleNamespace(
        command="cat /workspace/.reward_framework/submit_candidate.py",
        is_input=False,
    )
    assert _is_submit_command(reward)
    assert not _is_submit_command(unrelated)


def test_submission_bridge_uses_explicit_controller_address(monkeypatch):
    monkeypatch.setenv("OPENHANDS_EVAL_HOST_GATEWAY", "172.30.250.1")
    monkeypatch.setenv("REWARD_FRAMEWORK_CONTROLLER_HOST", "172.30.250.2")
    assert _submission_bridge_host() == "172.30.250.2"


def test_source_view_keeps_public_fuzz_driver_but_excludes_tests(tmp_path):
    (tmp_path / "fuzzers").mkdir()
    (tmp_path / "fuzzers/driver.c").write_text("int fuzz(void);", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/known_case.c").write_text("int testcase(void);", encoding="utf-8")
    relative = {path.relative_to(tmp_path).as_posix() for path in eligible_source_files(tmp_path)}
    assert "fuzzers/driver.c" in relative
    assert "tests/known_case.c" not in relative


def test_direct_submit_guard_allows_source_inspection():
    assert is_direct_submit_invocation("bash /workspace/submit.sh poc trace")
    assert is_direct_submit_invocation("cd /workspace && ./submit.sh poc trace")
    assert not is_direct_submit_invocation("cat /workspace/submit.sh")
    assert not is_direct_submit_invocation("grep submit.sh README.md")


def test_reward_framework_state_is_persisted_as_manifest_attempts(tmp_path):
    run_dir = tmp_path / "run"
    state = run_dir / "reward_framework"
    candidate = state / "candidates/candidate_0001_deadbeef/attempts/attempt_0001"
    candidate.mkdir(parents=True)
    (state / "task_context.json").write_text("{}", encoding="utf-8")
    (state / "candidates/candidate_0001_deadbeef/poc").write_bytes(b"poc")
    (candidate / "trace.json").write_text("[]", encoding="utf-8")
    (candidate / "current_runtime.json").write_text(
        json.dumps({"exit_code": 77, "trigger_observed": True}), encoding="utf-8"
    )
    (state / "candidates/index.json").write_text(json.dumps({
        "total_submissions": 1,
        "unique_candidates": 1,
        "by_sha256": {"deadbeef": "candidate_0001_deadbeef"},
        "attempts": [{
            "attempt_number": 1,
            "candidate_id": "candidate_0001_deadbeef",
            "sha256": "deadbeef",
            "duplicate_of": None,
        }],
    }), encoding="utf-8")
    (state / "evidence").mkdir()
    (state / "evidence/attempt_0001.json").write_text(json.dumps({
        "runtime": {"exit_code": 77, "trigger_observed": True},
        "assessment": {"first_unresolved": "propagation"},
        "feedback": {"summary": "triggered"},
    }), encoding="utf-8")
    (state / "observation_state.json").write_text(
        json.dumps({"terminal_reason": "trigger_success"}), encoding="utf-8"
    )

    sample = tmp_path / "sample"
    result = persist_reward_framework_state(run_dir, sample)
    assert result is not None
    summary, attempts = result
    assert summary["success"] is True
    assert summary["terminal_reason"] == "trigger_success"
    assert attempts[0]["trace_valid"] is True
    assert attempts[0]["trigger_observed"] is True
    assert attempts[0]["trace_path"].endswith("attempt_0001/trace.json")
    assert (sample / attempts[0]["evidence_path"]).is_file()


def test_transport_replays_terminal_success_without_duplicate_submission(tmp_path):
    workspace = tmp_path / "workspace"
    for attempt in ("a" * 32, "b" * 32):
        submission = workspace / ".reward_submissions" / attempt
        submission.mkdir(parents=True)
        (submission / "poc").write_bytes(b"same-poc")
        (submission / "trace.json").write_text("[]", encoding="utf-8")

    class Framework:
        def __init__(self):
            self.calls = 0
            self.terminal = None

        def status(self):
            return {"terminal_reason": self.terminal}

        def submit_candidate(self, _arguments):
            self.calls += 1
            self.terminal = "trigger_success"
            return {"triggered": True, "attempt_number": 1}

    framework = Framework()
    previous = os.environ.get("OPENHANDS_EVAL_HOST_GATEWAY")
    os.environ["OPENHANDS_EVAL_HOST_GATEWAY"] = "127.0.0.1"
    transport = OpenHandsRewardTransport(workspace_root=workspace, framework=framework)
    transport.start()
    try:
        results = []
        for attempt in ("a" * 32, "b" * 32):
            request = urllib.request.Request(
                transport.url + "/submit",
                data=json.dumps({"token": transport.token, "attempt_id": attempt}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                results.append(json.load(response))
        assert framework.calls == 1
        assert results[1]["replayed_after_terminal"] is True
        assert results[1]["attempt_number"] == 1
    finally:
        transport.close()
        if previous is None:
            os.environ.pop("OPENHANDS_EVAL_HOST_GATEWAY", None)
        else:
            os.environ["OPENHANDS_EVAL_HOST_GATEWAY"] = previous
