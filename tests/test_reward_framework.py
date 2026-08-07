import json
import os
import urllib.request
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
from reward_framework.runtime import StaticInstrumentationBackend
from reward_framework.runtime import default_trigger_oracle
from reward_framework.adapters.openhands import OpenHandsRewardTransport
from reward_framework.instrumentation.arvo import (
    ArvoGDBInstrumentationBackend,
    compile_checkpoints,
)
from reward_framework.models import Probe
from reward_framework.stage_evaluator import evaluate_stages
from reward_framework.submission_tool import resolve_workspace_path
from reward_framework.source_view import eligible_source_files


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
