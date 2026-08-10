from pathlib import Path

import pytest

from reward_framework.assertion_planner import plan_assertions
from reward_framework.assertion_reward import (
    AssertionRewardSpec,
    assess_assertions,
    validate_spec_sources,
)
from reward_framework.instrumentation.arvo import (
    compile_checkpoints,
    evaluate_claim_hits,
)


def _spec() -> AssertionRewardSpec:
    return AssertionRewardSpec.from_dict({
        "protocol": "assertion-reward-v1",
        "admission": [
            {"at": {"file": "foo.c", "function": "parse", "line": 1}},
            {"at": {"file": "foo.c", "function": "decode", "line": 4}},
        ],
        "claims": [
            {
                "kind": "required",
                "at": {"file": "foo.c", "function": "parse", "line": 2},
                "check": {"op": "le", "left": "length", "right": "capacity"},
            },
            {
                "kind": "observed",
                "at": {"file": "foo.c", "function": "parse", "line": 2},
                "check": {"op": "gt", "left": "length", "right": 0},
            },
            {
                "kind": "transition",
                "from": {"file": "foo.c", "function": "parse", "line": 2},
                "at": {"file": "foo.c", "function": "decode", "line": 5},
                "check": {"op": "same_object", "left": "buffer", "right": "buffer"},
            },
        ],
    })


def _source(tmp_path: Path) -> None:
    (tmp_path / "foo.c").write_text(
        "int parse(int length, int capacity) {\n"
        "  return length;\n"
        "}\n"
        "int decode(char *buffer) {\n"
        "  return *buffer;\n"
        "}\n",
        encoding="utf-8",
    )


def test_assertion_runtime_polarity_order_and_admission_or(tmp_path: Path):
    _source(tmp_path)
    spec = _spec()
    validate_spec_sources(spec, tmp_path)
    checkpoints = compile_checkpoints(tmp_path, plan_assertions(spec))
    # Hit only the second legal admission route: admission alternatives are OR.
    hits = [
        {"event_point": checkpoints[1]["event_point"], "line": 4, "fields": {}},
        {"event_point": checkpoints[2]["event_point"], "line": 2,
         "fields": {"left": 8, "right": 4}},
        {"event_point": checkpoints[3]["event_point"], "line": 2,
         "fields": {"left": 8}},
        {"event_point": checkpoints[4]["event_point"], "line": 2,
         "fields": {"left": 4096}},
        {"event_point": checkpoints[5]["event_point"], "line": 5,
         "fields": {"right": 4096}},
    ]
    admission, results = evaluate_claim_hits(checkpoints, hits, True)
    assert admission == "confirmed"
    assert [item.status for item in results] == [
        "violated", "confirmed", "confirmed"
    ]
    assert all(item.matched_vulnerable_state for item in results)


def test_transition_requires_temporal_order(tmp_path: Path):
    _source(tmp_path)
    checkpoints = compile_checkpoints(tmp_path, plan_assertions(_spec()))
    hits = [
        {"event_point": checkpoints[5]["event_point"], "line": 5,
         "fields": {"right": 4096}},
        {"event_point": checkpoints[4]["event_point"], "line": 2,
         "fields": {"left": 4096}},
    ]
    _, results = evaluate_claim_hits(checkpoints, hits, True)
    assert results[2].status == "unresolved"
    assert results[2].matched_vulnerable_state is None


def test_information_gain_is_relative_to_previous_distinct_candidate(tmp_path: Path):
    _source(tmp_path)
    checkpoints = compile_checkpoints(tmp_path, plan_assertions(_spec()))
    hits = [
        {"event_point": checkpoints[0]["event_point"], "line": 1, "fields": {}},
        {"event_point": checkpoints[2]["event_point"], "line": 2,
         "fields": {"left": 8, "right": 4}},
    ]
    admission, results = evaluate_claim_hits(checkpoints, hits, True)
    first = assess_assertions(
        admission=admission, results=results, previous=None,
        trigger_observed=False,
    )
    previous = {"assessment": first.to_dict()}
    repeated = assess_assertions(
        admission=admission, results=results, previous=previous,
        trigger_observed=False,
    )
    assert first.information_gain == 2
    assert repeated.information_gain == 0


def test_trigger_conflict_requires_every_claim_to_be_evaluated(tmp_path: Path):
    _source(tmp_path)
    checkpoints = compile_checkpoints(tmp_path, plan_assertions(_spec()))
    hits = [
        {"event_point": checkpoints[0]["event_point"], "line": 1, "fields": {}},
        {"event_point": checkpoints[2]["event_point"], "line": 2,
         "fields": {"left": 2, "right": 4}},
    ]
    admission, results = evaluate_claim_hits(checkpoints, hits, True)
    assessment = assess_assertions(
        admission=admission, results=results, previous=None,
        trigger_observed=True,
    )
    assert assessment.consistency == "consistent"


def test_side_effecting_operand_is_rejected(tmp_path: Path):
    _source(tmp_path)
    value = _spec().to_dict()
    value["claims"][0]["check"]["left"] = "read_length()"
    with pytest.raises(ValueError, match="unsafe claim operand"):
        validate_spec_sources(AssertionRewardSpec.from_dict(value), tmp_path)
