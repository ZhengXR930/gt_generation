import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from experiments.runtime_hypothesis_feedback.reward_agent import (
    decide_submission,
    public_reward_context,
    deterministic_fact_catalog,
    validate_diagnosis,
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


class _Response:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps({"choices": [{"message": self.message}]}).encode()


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


def test_readiness_role_receives_the_frozen_guidance_unchanged():
    """The readiness role must consume the frozen artifact, not re-derive it."""
    stages = {
        "root": {
            "claim": "the issue-relevant state is established",
            "where": [{"file": "parser.c", "function": "consume", "point": "entry"}],
            "observe": [],
        },
    }
    context = public_reward_context(
        {"claims": {}, "unknowns": []},
        reward_spec={"stages": stages},
    )
    assert context["frozen_reward_guidance"] == stages
