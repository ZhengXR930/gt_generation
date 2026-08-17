from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reward_framework import RewardFramework, StageStatus
from reward_framework.adapters.base import CallbackAdapter
from reward_framework.assertion_planner import plan_assertions
from reward_framework.assertion_reward import ClaimResult, RewardSpec, validate_spec_sources
from reward_framework.feedback_agent import FeedbackAgent
from reward_framework.runtime import RawRuntimeReport, RuntimeFact, StaticInstrumentationBackend
from reward_framework.stage_evaluator import evaluate_stages
from reward_framework.submission_tool import parse_submission


class FakeBackend:
    model = "fake-reward-agent"

    def __init__(self, spec: dict[str, Any] | None = None):
        self.spec = spec or _spec()
        self.roles: list[str] = []

    def run_json(self, *, role: str, prompt: str, schema: Path,
                 cwd: Path) -> dict[str, Any]:
        self.roles.append(role)
        return self.spec


def _spec() -> dict[str, Any]:
    return {
        "admission": [{
            "id": "admit_parser",
            "at": {"file": "parser.c", "function": "parse", "line": 2},
            "claim": "The public parser accepts the candidate buffer.",
            "operands": ["len"],
        }],
        "source": [{
            "id": "source_len",
            "at": {"file": "parser.c", "function": "parse", "line": 3},
            "claim": "The input length reaches the internal length variable.",
            "operands": ["len"],
        }],
        "root": [{
            "id": "root_overflow",
            "at": {"file": "parser.c", "function": "parse", "line": 5},
            "claim": "The requested copy length exceeds the destination capacity.",
            "check": {"op": "gt", "left": "len", "right": "cap"},
        }],
        "propagation": {
            "required": [{
                "id": "prop_len",
                "from": {"file": "parser.c", "function": "parse", "line": 3},
                "to": {"file": "parser.c", "function": "parse", "line": 6},
                "claim": "The same length controls the later copy.",
                "via": ["len"],
                "check": {"op": "same_object", "left": "len", "right": "len"},
            }],
            "optional": [],
        },
        "sink": [{
            "id": "sink_copy",
            "at": {"file": "parser.c", "function": "parse", "line": 6},
            "claim": "The copy consumes the oversized length.",
            "check": {"op": "gt", "left": "len", "right": "cap"},
        }],
    }


def _source(root: Path) -> Path:
    source = root / "src"
    source.mkdir()
    (source / "parser.c").write_text(
        "\n".join([
            "#include <string.h>",
            "int parse(const char *input, int len) {",
            "  int controlled = len;",
            "  char dst[8];",
            "  int cap = 8;",
            "  memcpy(dst, input, controlled);",
            "  return dst[0];",
            "}",
        ]) + "\n",
        encoding="utf-8",
    )
    return source


def _analysis(path: Path) -> None:
    path.write_text(json.dumps({
        "sample_id": "sample_1",
        "fine_trace": [
            {
                "step": 1,
                "file": "parser.c",
                "function": "parse",
                "line": 3,
                "var": "len",
                "code": "int controlled = len;",
                "role": "source",
                "note": "Input-controlled length becomes the internal copy length.",
            },
            {
                "step": 2,
                "file": "parser.c",
                "function": "parse",
                "line": 5,
                "var": "controlled > cap",
                "code": "int cap = 8;",
                "role": "root_cause",
                "note": "The fixed destination capacity must bound the copy length.",
            },
            {
                "step": 3,
                "file": "parser.c",
                "function": "parse",
                "line": 6,
                "var": "memcpy(dst, input, controlled)",
                "code": "memcpy(dst, input, controlled);",
                "role": "sink",
                "note": "The copy operation consumes the length against the fixed buffer.",
            },
        ],
        "vuln_logic": {
            "source": {"file": "parser.c", "function": "parse", "line": 3, "operands": ["len"]},
            "root_cause": {
                "file": "parser.c", "function": "parse", "line": 5,
                "operands": ["controlled", "cap"],
                "relation": {"op": "gt", "left": "controlled", "right": "cap"},
            },
            "sink": {
                "file": "parser.c", "function": "parse", "line": 6,
                "operands": ["controlled", "dst"],
                "relation": {"op": "gt", "left": "controlled", "right": "dst"},
            },
            "propagation": [],
            "issue_alignment": {
                "admission": "The candidate is intended to enter the parser as an input buffer.",
                "source": "The issue-relevant attacker-controlled length is represented by len.",
                "root_cause": "The bug condition concerns length exceeding the fixed capacity.",
                "propagation": "The same controlled length is used by the later copy.",
                "sink": "The dangerous operation is the copy into the fixed buffer.",
            },
        },
    }), encoding="utf-8")


def test_reward_spec_shape_and_probe_plan() -> None:
    spec = RewardSpec.from_dict(_spec())
    assert spec.constructable
    assert [claim.claim_id for claim in spec.admission] == ["admit_parser"]
    assert [claim.claim_id for claim in spec.propagation_required] == ["prop_len"]
    plan = plan_assertions(spec)
    stages = [probe.stage for probe in plan.probes]
    assert stages == [
        "admission", "source", "root", "propagation", "propagation", "sink",
    ]
    assert {probe.endpoint for probe in plan.probes if probe.stage == "propagation"} == {
        "from", "to",
    }


def test_parse_submission_requires_analysis_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "poc.bin").write_bytes(b"A")
    _analysis(workspace / "analysis.json")
    poc, analysis = parse_submission(
        workspace,
        {"poc_path": "/workspace/poc.bin", "analysis_path": "/workspace/analysis.json"},
    )
    assert poc == workspace / "poc.bin"
    assert analysis == workspace / "analysis.json"
    try:
        parse_submission(
            workspace,
            {"poc_path": "poc.bin", "trace_path": "candidate_trace.json"},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("legacy trace_path submission was accepted")


def test_stage_evaluator_uses_sink_not_target() -> None:
    spec = RewardSpec.from_dict(_spec())
    report = RawRuntimeReport(
        exit_code=0,
        stdout="",
        stderr="",
        trigger_observed=False,
        stage_observations={},
        facts=(RuntimeFact("TRIGGER", "trigger", "oracle", "No trigger.", {}),),
        instrumentation_available=True,
        claim_results=(
            ClaimResult("admit_parser", "admission", "confirmed", True, True, True, True),
            ClaimResult("source_len", "source", "confirmed", True, True, True, True),
            ClaimResult("root_overflow", "root", "confirmed", True, True, True, True),
            ClaimResult("prop_len", "propagation", "confirmed", True, True, True, True),
            ClaimResult("sink_copy", "sink", "confirmed", True, True, True, True),
        ),
    )
    assessment = evaluate_stages(spec, report)
    assert assessment.longest_confirmed_prefix == (
        "admission", "source", "root", "propagation", "sink",
    )
    assert assessment.stages["sink"] == StageStatus.CONFIRMED


def test_trigger_success_suppresses_negative_stage_refutation() -> None:
    spec = RewardSpec.from_dict(_spec())
    report = RawRuntimeReport(
        exit_code=134,
        stdout="",
        stderr="AddressSanitizer: heap-buffer-overflow",
        trigger_observed=True,
        stage_observations={},
        facts=(RuntimeFact("TRIGGER", "trigger", "oracle", "Trigger.", {}),),
        instrumentation_available=True,
        claim_results=(
            ClaimResult("admit_parser", "admission", "confirmed", True, True, True, True),
            ClaimResult("source_len", "source", "confirmed", True, True, True, True),
            ClaimResult("root_overflow", "root", "not_observed", True, True, False, False),
            ClaimResult("sink_copy", "sink", "not_observed", True, True, False, False),
        ),
    )
    assessment = evaluate_stages(spec, report)
    feedback = FeedbackAgent().generate(
        report=report, assessment=assessment, previous=None,
    )
    assert assessment.consistency == "spec_or_mapping_conflict"
    assert assessment.stages["root"] == StageStatus.SPEC_OR_MAPPING_CONFLICT
    assert assessment.stages["sink"] == StageStatus.SPEC_OR_MAPPING_CONFLICT
    assert feedback.contradiction is None
    assert "stage probes conflict" in feedback.summary


def test_reward_spec_member_access_validates_base_object_only(tmp_path: Path) -> None:
    source = _source(tmp_path)
    value = _spec()
    value["root"][0]["check"]["left"] = "len->field_from_header"
    validate_spec_sources(RewardSpec.from_dict(value), source)

    value["root"][0]["check"]["left"] = "missing_base->field_from_header"
    try:
        validate_spec_sources(RewardSpec.from_dict(value), source)
    except ValueError as exc:
        assert "missing_base" in str(exc)
    else:
        raise AssertionError("missing member-access base was accepted")


def test_framework_submit_candidate_records_analysis_and_feedback(tmp_path: Path) -> None:
    source = _source(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "poc.bin").write_bytes(b"AAAA")
    _analysis(workspace / "analysis.json")
    injected: list[str] = []
    report = RawRuntimeReport(
        exit_code=0,
        stdout="",
        stderr="",
        trigger_observed=False,
        stage_observations={},
        facts=(RuntimeFact("TRIGGER", "trigger", "oracle", "No trigger.", {}),),
        instrumentation_available=True,
        claim_results=(
            ClaimResult("admit_parser", "admission", "confirmed", True, True, True, True),
            ClaimResult("source_len", "source", "confirmed", True, True, True, True),
            ClaimResult("root_overflow", "root", "not_observed", True, True, False, False),
        ),
    )
    framework = RewardFramework.create(
        task_id="sample_1",
        issue_description="Parser copies an attacker-controlled length into a fixed buffer.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(report),
        platform=CallbackAdapter(
            workspace_root=workspace,
            inject=injected.append,
            checkpoint_callback=lambda label: tmp_path / "checkpoint" / label,
        ),
    )
    result = framework.submit_candidate({
        "poc_path": "poc.bin",
        "analysis_path": "analysis.json",
    })
    assert result["triggered"] is False
    assert result["assessment"]["stages"]["sink"] in {"not_reached", "not_declared"}
    assert result["feedback"]["issue_alignment_review"]["llm_judge_used"] is False
    assert result["feedback"]["issue_alignment_review"]["stages"][0]["stage"] == "admission"
    assert "Submitted issue-alignment" in result["feedback"]["summary"]
    latest = list((tmp_path / "state/candidates").glob("candidate_0001_*/latest_analysis.json"))
    assert len(latest) == 1
    assert any(message.startswith("[Runtime reward evidence]") for message in injected)
    summary = framework.finalize_episode()
    assert json.loads(summary.read_text())["gt_used"] is False


def test_framework_auto_submission_tracks_exact_candidate_bundle(tmp_path: Path) -> None:
    source = _source(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "poc.bin").write_bytes(b"AAAA")
    _analysis(workspace / "analysis.json")
    framework = RewardFramework.create(
        task_id="sample_1",
        issue_description="Parser copies an attacker-controlled length into a fixed buffer.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            exit_code=0,
            stdout="",
            stderr="",
            trigger_observed=False,
            stage_observations={},
            facts=(),
            instrumentation_available=True,
        )),
        platform=CallbackAdapter(workspace_root=workspace),
    )

    assert framework.auto_submission_needed(iteration=3, maximum=100) is True
    assert framework.auto_submission_needed(iteration=4, maximum=100) is False

    analysis = json.loads((workspace / "analysis.json").read_text())
    analysis["fine_trace"][0]["note"] = "repaired analysis for same PoC"
    (workspace / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")

    assert framework.auto_submission_needed(iteration=5, maximum=100) is True
    state = framework.store.load_observation()
    assert state.auto_submission_count == 2
    assert [
        event.kind for event in state.events
        if event.kind == "auto_submission_scheduled"
    ] == ["auto_submission_scheduled", "auto_submission_scheduled"]


def test_framework_materialization_checkpoint_is_one_shot(tmp_path: Path) -> None:
    source = _source(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    injected: list[str] = []
    framework = RewardFramework.create(
        task_id="sample_1",
        issue_description="Parser copies an attacker-controlled length into a fixed buffer.",
        codebase_root=source,
        state_dir=tmp_path / "state",
        backend=FakeBackend(),
        instrumentation=StaticInstrumentationBackend(RawRuntimeReport(
            exit_code=0,
            stdout="",
            stderr="",
            trigger_observed=False,
            stage_observations={},
            facts=(),
            instrumentation_available=True,
        )),
        platform=CallbackAdapter(
            workspace_root=workspace,
            inject=injected.append,
            submission_ready_callback=lambda: False,
        ),
    )

    assert framework.materialization_reminder_needed(
        iteration=8,
        maximum=100,
        thought=(
            "Now I understand the vulnerable path. To trigger the "
            "uninitialized decoder, the input should be a zero-length payload."
        ),
        command="grep -rn decoder repo-vul/src-vul",
    ) is False
    assert injected and injected[-1].startswith("[Candidate materialization checkpoint]")
    status = framework.status()
    assert status["materialization_outstanding"] is True
    assert status["materialization_reminder_count"] == 1

    assert framework.materialization_reminder_needed(
        iteration=9,
        maximum=100,
        thought="The PoC input should be the same bytes.",
        command="grep -rn parser repo-vul/src-vul",
    ) is False
    assert framework.materialization_gate_blocks_action(
        iteration=9,
        maximum=100,
        command="grep -rn parser repo-vul/src-vul",
    ) is False
    assert framework.materialization_gate_blocks_action(
        iteration=10,
        maximum=100,
        command="find /workspace/repo-vul/src-vul -type f | head -100",
    ) is True
    assert injected[-1].startswith("[Candidate materialization gate]")
    assert framework.materialization_gate_blocks_action(
        iteration=11,
        maximum=100,
        command="printf 'AAAA' > /workspace/poc.bin && cat > /workspace/analysis.json <<'JSON'\n{}\nJSON",
    ) is False
