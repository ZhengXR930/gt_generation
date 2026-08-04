import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from experiments.runtime_hypothesis_feedback.reward_agent import (
    SCHEMA_VERSION,
    SourceTools,
    decide_submission,
    deterministic_fact_catalog,
    enforce_public_evidence_boundary,
    generate_reward_map,
    validate_diagnosis,
    validate_reward_map,
)


def test_fact_catalog_exposes_anchor_contradiction_and_call_values():
    facts = deterministic_fact_catalog({
        "runtime_checked": True,
        "step_evidence": [{
            "step": 3,
            "exact_hit": False,
            "function_hit": True,
            "observed_file": "decoder.cpp",
            "observed_function": "Load",
            "observed_line": 10,
            "anchor_validation": {
                "status": "invalid",
                "reason": "line_code_mismatch",
                "matching_lines": [42],
            },
        }],
        "runtime_call_observations": [{
            "call_name": "readRawData",
            "requested_bytes": 8,
            "returned_bytes": 3,
            "short_read": True,
            "branch_facts": {"packet_is_rle": False},
        }],
    }, {"stage_state": {
        "admission": "confirmed", "root": "contradicted",
        "propagation": "not_reached", "target": "not_reached",
    }})
    assert "contradicted by the public vulnerable source" in (
        facts["step.3.anchor_contradiction"]
    )
    assert '"returned_bytes": 3' in facts["call.1"]


def _codebase(root: Path) -> None:
    (root / "parser.c").write_text(
        "int parse_input(char *data) { return consume(data); }\n"
        "int consume(char *data) { return data[0]; }\n",
        encoding="utf-8",
    )


def _map():
    return {
        "admission": {
            "claim": "the parser accepts the input",
            "anchors": [{"file": "parser.c", "function": "parse_input"}],
            "observation": "parse_input is entered",
        },
        "root": {
            "claim": "the issue-relevant state is established",
            "anchors": [{"file": "parser.c", "function": "consume"}],
            "observation": "the issue-stated safety property is violated",
        },
        "propagation": {
            "mode": "collapsed_with_target",
            "claim": "the Root operation is the dangerous consumption",
            "anchors": [{"file": "parser.c", "function": "consume"}],
            "observation": "",
        },
        "target": {
            "claim": "the authoritative runtime violation occurs",
            "anchors": [],
            "observation": "authoritative runtime oracle",
        },
    }


def test_reward_map_anchors_must_exist_in_public_codebase():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _codebase(root)
        tools = SourceTools(root)
        result = validate_reward_map(_map(), tools)
        assert result["propagation"]["mode"] == "collapsed_with_target"
        invalid = _map()
        invalid["root"]["anchors"][0]["function"] = "invented_function"
        try:
            validate_reward_map(invalid, tools)
        except ValueError as exc:
            assert "absent" in str(exc)
        else:
            raise AssertionError("invented source anchor should be rejected")


def test_distinct_propagation_requires_issue_named_consumers():
    reward_map = _map()
    reward_map["propagation"] = {
        "mode": "distinct",
        "claim": "A later parser consumer uses the state.",
        "anchors": [{"file": "parser.c", "function": "parse_input"}],
        "observation": "The consumer executes.",
    }
    bounded, downgrades = enforce_public_evidence_boundary(
        reward_map, "An accepted input establishes an invalid length.",
    )
    assert bounded["propagation"]["mode"] == "not_declared"
    assert downgrades == ["propagation_not_publicly_anchored"]

    retained, downgrades = enforce_public_evidence_boundary(
        reward_map, "The invalid length is consumed by parse_input.",
    )
    assert retained["propagation"]["mode"] == "distinct"
    assert downgrades == []


def test_precise_target_anchor_requires_public_issue_support():
    reward_map = _map()
    reward_map["target"] = {
        "claim": "consume performs the violation",
        "anchors": [{"file": "parser.c", "function": "consume"}],
        "observation": "authoritative runtime oracle in consume",
    }
    bounded, downgrades = enforce_public_evidence_boundary(
        reward_map, "The parser has a memory-safety issue.",
    )
    assert bounded["target"]["anchors"] == []
    assert bounded["target"]["observation"] == "authoritative runtime oracle"
    assert downgrades == ["target_not_publicly_anchored"]


class _Response:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps({"choices": [{"message": self.message}]}).encode()


def test_initialization_role_uses_read_only_code_tool_then_freezes_map():
    payloads = []
    tool_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "search_code",
                "arguments": json.dumps({"query": "parse_input"}),
            },
        }],
    }
    final_message = {"role": "assistant", "content": json.dumps(_map())}
    responses = iter((
        _Response(tool_message),
        _Response(final_message),
        _Response(final_message),
    ))

    def fake_urlopen(request, timeout):
        payloads.append(json.loads(request.data.decode("utf-8")))
        return next(responses)

    with TemporaryDirectory() as directory:
        root = Path(directory)
        _codebase(root)
        with patch(
            "experiments.runtime_hypothesis_feedback.reward_agent.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = generate_reward_map(
                sample_id="sample", issue_text="public issue", codebase=root,
                api_key="key", max_rounds=3,
            )
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["provenance"]["uses_hidden_gt"] is False
    assert "parser.c" in result["source_audit"]["files_read"]
    assert payloads[0]["tool_choice"] == "auto"
    assert "tools" in payloads[0]
    assert "tools" not in payloads[1]
    assert payloads[1]["response_format"] == {"type": "json_object"}
    assert "independent source-audit role" in payloads[2]["messages"][0]["content"]
    assert "DRAFT REWARD MAP (untrusted)" in payloads[2]["messages"][1]["content"]
    assert payloads[2]["tool_choice"] == "auto"


def test_diagnosis_preserves_unresolved_as_distinct_from_contradicted():
    value = validate_diagnosis({
        "last_confirmed": "Admission",
        "first_unresolved": "Root",
        "stage_assessment": {
            "admission": "confirmed",
            "root": "unresolved",
            "propagation": "collapsed_with_target",
            "target": "not_reached",
        },
        "evidence_ids": ["step.2.hit"],
        "delta": "No new Root evidence was observed.",
        "reason": "The required state relation was unavailable.",
    }, fact_catalog={"step.2.hit": "The Root location was reached."})
    assert value["stage_assessment"]["root"] == "unresolved"
    assert value["runtime_facts"] == ["The Root location was reached."]


def test_diagnosis_rejects_uncited_numeric_runtime_claims():
    try:
        validate_diagnosis({
            "last_confirmed": "admission",
            "first_unresolved": "root",
            "stage_assessment": {
                "admission": "confirmed",
                "root": "unresolved",
                "propagation": "observed_but_blocked",
                "target": "not_reached",
            },
            "evidence_ids": ["stage.root"],
            "delta": "No prior candidate.",
            "reason": "The observed pointer 18109411 is below 5844896.",
        }, fact_catalog={"stage.root": "Root evidence state: unresolved."})
    except ValueError as exc:
        assert "uncited numeric runtime claims" in str(exc)
    else:
        raise AssertionError("uncited numeric runtime claim was accepted")


def test_readiness_role_uses_the_same_frozen_reward_map():
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs["payload"])
        return {"choices": [{"message": {"content": '{"decision":"submit"}'}}]}

    with patch(
        "experiments.runtime_hypothesis_feedback.reward_agent._request",
        side_effect=fake_request,
    ):
        decision = decide_submission(
            skeleton={"claims": {}, "unknowns": []},
            reward_spec={"reward_map": _map()},
            raw_trajectory="observed harness and candidate representation",
            api_key="key",
        )
    assert decision == {"decision": "submit"}
    assert "JSON" in captured["messages"][0]["content"]
    user_context = json.loads(captured["messages"][1]["content"])
    assert user_context["task_reward_state"]["frozen_reward_map"] == _map()
