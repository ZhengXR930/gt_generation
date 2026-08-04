import json
from unittest.mock import patch

from experiments.runtime_hypothesis_feedback.lightweight_reward import (
    SYSTEM_PROMPT,
    call_lightweight_reward,
    public_trace,
    validate_guidance,
    verifier_evidence,
)


def test_reporter_forbids_unverified_raw_value_comparisons():
    prompt = " ".join(SYSTEM_PROMPT.split())
    assert "only when that relation is supplied verbatim" in prompt
    assert "scalar_or_pointer is ambiguous" in prompt


def test_reporter_forbids_checkpoint_frequency_inference():
    prompt = " ".join(SYSTEM_PROMPT.split())
    assert "do not mean that the program executed that line twice" in prompt
    assert "never a loop, branch, call, or event count" in prompt
    assert "not a complete control-flow trace" in prompt


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "last_confirmed": "Admission.",
                        "first_failed": "Root condition.",
                        "reason": "The Root location was hit but its claimed state was not observed.",
                    })
                }
            }]
        }).encode()


def test_public_trace_contains_only_bounded_agent_claims():
    trace = [{
        "file": "parser.c",
        "function": "parse",
        "note": "agent hypothesis",
        "captures": {"secret": "not forwarded"},
        "condition": {"op": "eq", "left": "secret", "right": 1},
    }]
    assert public_trace(trace) == [{
        "step": 1,
        "file": "parser.c",
        "function": "parse",
        "role": "",
        "phase": "",
        "claim": "agent hypothesis",
        "condition": {"op": "eq", "left": "secret", "right": 1},
    }]


def test_verifier_evidence_forwards_bounded_runtime_values_and_failures():
    evidence = verifier_evidence({
        "duplicate_poc": False,
        "new_runtime_evidence": True,
        "runtime_checked": True,
        "reward": {
            "admission": "confirmed",
            "root": "location_reached_only",
            "propagation": "consumer_not_reached",
            "target": "not_triggered",
        },
        "steps": [{
            "step": 2,
            "exact_hit": True,
            "observed_file": "/src/parser.c",
            "observed_function": "parse",
            "fields": {"raw_pointer": "not forwarded"},
            "capture_errors": {"length": "optimized out"},
            "condition_satisfied": False,
        }],
    })
    step = evidence["step_evidence"][0]
    assert step["observed_function"] == "parse"
    assert step["captured_values"] == {"raw_pointer": "not forwarded"}
    assert step["capture_errors"] == {"length": "optimized out"}
    assert step["condition_satisfied"] is False


def test_verifier_evidence_does_not_expose_breakpoint_counts_as_execution_counts():
    evidence = verifier_evidence({
        "steps": [
            {
                "step": 7,
                "observed_line": 345,
                "observed_sequence": 11,
                "observed_hit_count": 1,
                "checkpoint_sample_ordinal": 1,
            },
            {
                "step": 8,
                "observed_line": 345,
                "observed_sequence": 12,
                "observed_hit_count": 1,
                "checkpoint_sample_ordinal": 1,
            },
        ],
    })
    assert all(
        "observed_hit_count" not in step and "checkpoint_sample_ordinal" not in step
        for step in evidence["step_evidence"]
    )
    assert evidence["step_evidence_semantics"] == {
        "scope": "bounded_checkpoint_observations_per_declared_trace_step",
        "program_execution_counts_available": False,
        "observed_sequence_is_complete_control_flow": False,
        "same_location_steps_are_independent_checkpoints": True,
    }


def test_call_is_one_json_llm_request_without_source_context():
    with patch(
        "experiments.runtime_hypothesis_feedback.lightweight_reward.urllib.request.urlopen",
        return_value=_Response(),
    ) as urlopen:
        result = call_lightweight_reward(
            issue_text="Public issue",
            trace=[{"function": "parse", "note": "input reaches parser"}],
            feedback={
                "runtime_checked": True,
                "reward": {
                    "admission": "confirmed",
                    "root": "not_reached",
                    "propagation": "blocked_on_root",
                    "target": "not_triggered",
                },
            },
            runtime_output="Execution successful",
            api_key="test-key",
        )
    assert result["first_failed"] == "Root condition."
    request = urlopen.call_args.args[0]
    body = json.loads(request.data)
    context = json.loads(body["messages"][1]["content"])
    assert set(context) == {
        "public_issue",
        "submitted_fine_trace",
        "deterministic_runtime_evidence",
        "candidate_runtime_output",
    }
    assert "source_code" not in context


def test_guidance_requires_three_nonempty_short_fields():
    assert validate_guidance({
        "last_confirmed": "a",
        "first_failed": "b",
        "reason": "c",
    }) == {"last_confirmed": "a", "first_failed": "b", "reason": "c"}
    try:
        validate_guidance({
            "last_confirmed": "a", "first_failed": "b", "reason": ""
        })
    except ValueError as exc:
        assert "reason" in str(exc)
    else:
        raise AssertionError("empty guidance should fail")
