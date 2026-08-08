import json
import os
import urllib.request
from types import SimpleNamespace
from pathlib import Path

from reward_framework.adapters.base import CallbackAdapter
from reward_framework.models import (
    ProbePlan,
    RawRuntimeReport,
    RewardSpec,
    RuntimeFact,
    SourceAnchor,
    StageStatus,
)
from reward_framework.orchestrator import RewardFramework
from reward_framework.cross_sample import CrossSampleHarnessPatcher
from reward_framework.episode_analyzer import EpisodeAnalyzer, collect_episode_metrics
from reward_framework.experience_pool import ExperiencePool
from reward_framework.harness_repository import HarnessRepository
from reward_framework.runtime import StaticInstrumentationBackend
from reward_framework.runtime import default_trigger_oracle
from reward_framework.adapters.openhands import (
    OpenHandsRewardTransport,
    is_direct_submit_invocation,
)
from reward_framework.instrumentation.arvo import (
    ArvoGDBInstrumentationBackend,
    compile_checkpoints,
)
from reward_framework.models import Probe
from reward_framework.stage_evaluator import evaluate_stages
from reward_framework.submission_tool import resolve_workspace_path
from reward_framework.source_view import eligible_source_files
from reward_framework.feedback_agent import fallback_feedback
from reward_framework.backend import CodexBackend
from poc_generation.poc_generator.run_sample import persist_reward_framework_state
from poc_generation.poc_generator.run_openhands_cybergym import (
    configure_harness_profile,
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
    experience = EpisodeAnalyzer(backend).analyze(
        store=framework.store, harness_version=7
    )
    pool = ExperiencePool(tmp_path / "experience_pool")
    pool.append(experience)
    optimizer_view = pool.optimizer_view()
    assert optimizer_view["episodes"][0]["metrics"]["harness_version"] == 7
    serialized = json.dumps(optimizer_view)
    assert "oversized parser" not in serialized.lower()
    assert "parser.c" not in serialized


def test_cross_sample_patcher_edits_real_fork_for_next_episode(tmp_path):
    pristine = tmp_path / "pristine"
    controller = pristine / "openhands/controller"
    controller.mkdir(parents=True)
    core_file = controller / "agent_controller.py"
    core_file.write_text("class AgentController: pass\n", encoding="utf-8")
    repository = HarnessRepository(tmp_path / "training/harness", pristine)
    assert repository.initialize() == 1
    pool = ExperiencePool(tmp_path / "training/experience_pool")
    pool.append({
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
    })

    class RealSourcePatcher:
        model = "fake-patcher"

        def run_json(self, *, role, prompt, schema, cwd):
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
    (submission / "trace.json").write_text("[]", encoding="utf-8")

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
    finally:
        transport.close()
        if previous is None:
            os.environ.pop("OPENHANDS_EVAL_HOST_GATEWAY", None)
        else:
            os.environ["OPENHANDS_EVAL_HOST_GATEWAY"] = previous


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
