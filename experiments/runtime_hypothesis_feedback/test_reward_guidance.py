"""Tests for the deterministic half of reward guidance v2.

Drafting and auditing need a model, but validation and agreement do not, and
those two are exactly where v1's failures were: a schema that accepted source
expressions from the model, and a trust label the model wrote about itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import reward_guidance as rg  # noqa: E402


@pytest.fixture()
def source(tmp_path: Path) -> rg.SourceTools:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "parser.c").write_text(
        "int parse_header(struct ctx *c) {\n"
        "    int n = read_u32(c);\n"
        "    return consume_chunk(c, n);\n"
        "}\n",
        encoding="utf-8",
    )
    return rg.SourceTools(root)


def _stage(function: str = "parse_header", point: str = "entry", observe=None) -> dict:
    return {
        "claim": "the parsed element count is established",
        "where": [{"file": "src/parser.c", "function": function, "point": point}],
        "observe": observe if observe is not None else [],
    }


def _guidance(**overrides) -> dict:
    value = {
        "admission": _stage(),
        "root": _stage(point="call:consume_chunk"),
        "propagation": {**_stage(), "mode": "collapsed_with_target"},
        "target": _stage(point="return"),
    }
    value.update(overrides)
    return value


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

def test_accepts_semantic_guidance(source):
    result = rg.validate_guidance(_guidance(), source)
    assert set(result) == set(rg.STAGES)
    assert result["root"]["where"][0]["point"] == "call:consume_chunk"


def test_rejects_line_number_style_point(source):
    with pytest.raises(ValueError, match="point must be"):
        rg.validate_guidance(_guidance(admission=_stage(point="42")), source)


def test_rejects_call_point_whose_callee_is_absent(source):
    with pytest.raises(ValueError, match="callee"):
        rg.validate_guidance(_guidance(admission=_stage(point="call:no_such_fn")), source)


def test_rejects_absent_function(source):
    with pytest.raises(ValueError, match="absent"):
        rg.validate_guidance(_guidance(admission=_stage(function="nope")), source)


@pytest.mark.parametrize(
    "name",
    ["hdr->length", "ctx.data[0]", "read_u32(c)", "obj::field"],
)
def test_rejects_source_expression_as_observation_name(source, name):
    """The whole point of v2: the model states semantics, not C syntax."""
    observe = [{"name": name, "why": "issue says so", "expect": "gt", "operand": 0}]
    with pytest.raises(ValueError, match="prose"):
        rg.validate_guidance(_guidance(root=_stage(observe=observe)), source)


def test_comparison_relation_requires_operand(source):
    observe = [{"name": "the parsed element count", "why": "x", "expect": "gt"}]
    with pytest.raises(ValueError, match="needs an operand"):
        rg.validate_guidance(_guidance(root=_stage(observe=observe)), source)


def test_relation_must_come_from_the_fixed_vocabulary(source):
    observe = [{"name": "the parsed element count", "why": "x", "expect": "looks_wrong"}]
    with pytest.raises(ValueError, match="expect must be"):
        rg.validate_guidance(_guidance(root=_stage(observe=observe)), source)


def test_observation_without_location_is_rejected(source):
    stage = {
        "claim": "c",
        "where": [],
        "observe": [{"name": "the count", "why": "x", "expect": "uninitialized"}],
    }
    with pytest.raises(ValueError, match="no location"):
        rg.validate_guidance(_guidance(root=stage), source)


def test_not_declared_propagation_must_not_carry_evidence(source):
    stage = {**_stage(), "mode": "not_declared"}
    with pytest.raises(ValueError, match="must not carry evidence"):
        rg.validate_guidance(_guidance(propagation=stage), source)


# --------------------------------------------------------------------------- #
# Pass 3: agreement                                                            #
# --------------------------------------------------------------------------- #

def _sample(root_function: str, observe=None) -> dict:
    return {
        "admission": _stage(),
        "root": _stage(function=root_function, observe=observe),
        "propagation": {**_stage(), "mode": "collapsed_with_target"},
        "target": _stage(point="return"),
    }


def test_unanimous_anchors_are_labelled_agreed():
    merged, support = rg.agreement_support([_sample("parse_header")] * 3)
    assert support["root"] == "agreed_3_of_3"
    assert merged["root"]["where"][0]["function"] == "parse_header"


def test_majority_wins_and_is_labelled_as_majority():
    samples = [_sample("parse_header"), _sample("parse_header"), _sample("other_fn")]
    merged, support = rg.agreement_support(samples)
    assert support["root"] == "majority_2_of_3"
    assert merged["root"]["where"][0]["function"] == "parse_header"


def test_three_way_disagreement_empties_the_stage():
    """No majority means the issue does not determine the binding, so the stage
    must produce diagnostics only -- never reward."""
    samples = [_sample("a_fn"), _sample("b_fn"), _sample("c_fn")]
    merged, support = rg.agreement_support(samples)
    assert support["root"] == rg.SUPPORT_AMBIGUOUS
    assert merged["root"]["where"] == []
    assert merged["root"]["observe"] == []


def test_lone_observation_is_dropped_even_when_anchors_agree():
    """A single run must not smuggle in an expectation the others did not make."""
    with_obs = [{"name": "the parsed element count", "why": "x", "expect": "gt", "operand": 0}]
    samples = [
        _sample("parse_header", observe=with_obs),
        _sample("parse_header"),
        _sample("parse_header"),
    ]
    merged, support = rg.agreement_support(samples)
    assert support["root"] == "agreed_3_of_3"
    assert merged["root"]["observe"] == []


def test_shared_observation_survives():
    obs = [{"name": "the parsed element count", "why": "x", "expect": "gt", "operand": 0}]
    samples = [_sample("parse_header", observe=obs) for _ in range(3)]
    merged, _ = rg.agreement_support(samples)
    assert merged["root"]["observe"][0]["expect"] == "gt"


def test_propagation_minority_mode_collapses_to_not_declared():
    a = _sample("parse_header")
    a["propagation"] = {**_stage(), "mode": "distinct"}
    b = _sample("parse_header")
    b["propagation"] = {"claim": "c", "where": [], "observe": [], "mode": "not_declared"}
    c = _sample("parse_header")
    c["propagation"] = {"claim": "c", "where": [], "observe": [], "mode": "not_declared"}
    merged, support = rg.agreement_support([a, b, c])
    assert merged["propagation"]["mode"] == "not_declared"
    assert support["propagation"] == rg.SUPPORT_NOT_DECLARED


def test_agreement_needs_at_least_one_sample():
    with pytest.raises(ValueError, match="at least one"):
        rg.agreement_support([])
