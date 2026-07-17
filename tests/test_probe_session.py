import json

from reasoning.session import (
    PROBE_MARKER,
    build_probe_prompt,
    grade_probe_response,
    load_public_probes,
    parse_probe_response,
)


def test_oracle_answers_are_not_loaded_into_subject_prompt(tmp_path):
    path = tmp_path / "oracle.json"
    path.write_text(json.dumps({
        "schema_version": "reasoning-probes-v1",
        "probes": [
            {
                "id": "reach",
                "dimension": "Reach",
                "question": "Reach: ___",
                "answer": "secret_sink",
            },
            {
                "id": "mechanism",
                "dimension": "Mechanism",
                "question": "Mechanism: ___",
                "answer": "secret_condition",
            },
            {
                "id": "propagation",
                "dimension": "Propagation",
                "question": "Propagation: ___P1___",
                "answer": "secret_relation",
            },
        ],
    }))

    public = load_public_probes(path)
    prompt = build_probe_prompt(public, "poc_submitted")

    assert public == [
        {"id": "q001", "question": "Reach: ___"},
        {"id": "q002", "question": "Mechanism: ___"},
        {"id": "q003", "question": "Propagation: ___P1___"},
    ]
    assert "secret_condition" not in prompt
    assert "secret_relation" not in prompt
    assert PROBE_MARKER in prompt
    assert "tools and environment access are disabled" in prompt


def test_stale_probe_artifact_is_not_loaded(tmp_path):
    path = tmp_path / "oracle.json"
    path.write_text(json.dumps({"schema_version": "assertion-probes-v2", "probes": []}))

    try:
        load_public_probes(path)
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale probe artifact was accepted")


def test_probe_response_parser_accepts_plain_or_fenced_json():
    expected = {"answers": [{"id": "q1", "answer": "x < n"}]}
    assert parse_probe_response(json.dumps(expected)) == expected
    assert parse_probe_response("```json\n" + json.dumps(expected) + "\n```") == expected


def test_probe_grading_is_deterministic_and_does_not_return_gold(tmp_path):
    oracle = tmp_path / "oracle.json"
    oracle.write_text(json.dumps({"probes": [
        {"id": "semantic.required", "question": "Q", "answer": "raw_count >= -3"},
        {"id": "semantic.observed", "question": "Q", "answer": "ptr != NULL"},
    ]}))
    response = json.dumps({"answers": [
        {"id": "q001", "answer": "raw_count>=-3"},
        {"id": "q002", "answer": "ptr == NULL"},
    ]})

    grade = grade_probe_response(oracle, response)

    assert grade["score"] == 0.5
    assert grade["items"] == [
        {"id": "q001", "correct": True},
        {"id": "q002", "correct": False},
    ]
    assert "raw_count" not in json.dumps(grade)


def test_required_probe_accepts_missing_atom_inside_complete_condition(tmp_path):
    oracle = tmp_path / "oracle.json"
    oracle.write_text(json.dumps({"probes": [{
        "id": "required.semantic",
        "answer_mode": "required_atom",
        "question": "Q",
        "answer": "raw_count >= -3",
    }]}))
    response = json.dumps({"answers": [{
        "id": "q001", "answer": "-3 <= raw_count && raw_count <= 3"
    }]})
    assert grade_probe_response(oracle, response)["score"] == 1.0


def test_whole_chain_requires_every_relation_in_order(tmp_path):
    oracle = tmp_path / "oracle.json"
    oracle.write_text(json.dumps({"probes": [{
        "id": "chain.semantic",
        "answer_mode": "ordered_relations",
        "question": "Q",
        "answer": "a == b -> n > cap -> after != before",
    }]}))
    complete = json.dumps({"answers": [{
        "id": "q001", "answer": "a == b; n > cap; after != before"
    }]})
    partial = json.dumps({"answers": [{
        "id": "q001", "answer": "a == b; n > cap"
    }]})
    assert grade_probe_response(oracle, complete)["score"] == 1.0
    assert grade_probe_response(oracle, partial)["score"] == 0.0
