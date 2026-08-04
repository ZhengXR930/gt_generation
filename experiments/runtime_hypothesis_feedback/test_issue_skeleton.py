import json

import pytest

from experiments.runtime_hypothesis_feedback.issue_skeleton import (
    UNKNOWN_FIELDS,
    build_skeleton,
    render_prompt,
    validate_generated,
)


ISSUE = "The decoder accepts invalid UTF-8 because no validation is performed."


def generated():
    return {
        "claims": {
            "operation": {"value": "decoder", "evidence_text": "decoder"},
            "affected_value": {"value": "UTF-8", "evidence_text": "UTF-8"},
            "expected_property": {"value": "valid UTF-8", "evidence_text": "invalid UTF-8"},
            "claimed_violation": {"value": "accepts invalid UTF-8", "evidence_text": "accepts invalid UTF-8"},
            "missing_enforcement": {"value": "no validation", "evidence_text": "no validation"},
        },
        "root_hypothesis": {
            "predicate": "invalid UTF-8 is accepted",
            "positive_evidence": ["invalid UTF-8 reaches output"],
            "insufficient_evidence": ["the decoder merely returns data"],
        },
    }


def test_validation_requires_verbatim_issue_evidence():
    bad = generated()
    bad["claims"]["operation"]["evidence_text"] = "not in issue"
    normalized = validate_generated(ISSUE, bad)
    assert normalized["claims"]["operation"] == {
        "value": None,
        "evidence_text": None,
    }
    assert normalized["validation_warnings"]


def test_incomplete_claim_pair_is_downgraded_to_unknown():
    incomplete = generated()
    incomplete["claims"]["expected_property"] = {
        "value": "must remain bounded",
        "evidence_text": None,
    }
    normalized = validate_generated(ISSUE, incomplete)
    assert normalized["claims"]["expected_property"] == {
        "value": None,
        "evidence_text": None,
    }
    assert "incomplete value/evidence pair" in normalized["validation_warnings"][0]


def test_builder_preserves_issue_and_forces_sensitive_unknowns(tmp_path):
    issue_path = tmp_path / "description.txt"
    output_path = tmp_path / "skeleton.json"
    issue_path.write_text(ISSUE, encoding="utf-8")
    before = issue_path.read_bytes()

    artifact = build_skeleton(
        "arvo_test",
        issue_path,
        output_path,
        "unused",
        generator=lambda _issue, _key: generated(),
    )

    assert issue_path.read_bytes() == before
    assert artifact["unknowns"] == list(UNKNOWN_FIELDS)
    assert artifact["provenance"]["uses_hidden_gt"] is False
    assert json.loads(output_path.read_text()) == artifact


def test_rendered_prompt_labels_secondary_artifact():
    artifact = {
        "schema_version": "issue-skeleton-v2",
        "claims": generated()["claims"],
        "root_hypothesis": generated()["root_hypothesis"],
        "unknowns": list(UNKNOWN_FIELDS),
        "action_boundary": {
            "first_candidate_scope": "submit a root candidate",
            "not_prerequisites_for_first_submission": ["downstream_consumer"],
            "after_first_feedback": "revise",
        },
        "provenance": {"uses_hidden_gt": False},
    }
    prompt = render_prompt("base", artifact)
    assert prompt.startswith("base")
    assert "secondary, issue-only restatement" in prompt
    assert '"downstream_consumer"' in prompt
