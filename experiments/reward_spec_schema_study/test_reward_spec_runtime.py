from experiments.reward_spec_schema_study.reward_spec_runtime import (
    RewardSpecError,
    compile_checkpoints,
    summarize_reward,
    validate_spec,
)


def _contract(goal, event_id, line, capture, predicate, observability="direct"):
    return {
        "goal": goal,
        "observability": observability,
        "events": [
            {
                "id": event_id,
                "at": f"src/demo.c:demo:{line}",
                "capture": [
                    {"name": name, "expression": expression}
                    for name, expression in capture.items()
                ],
            }
        ],
        "predicate": predicate,
    }


def _spec():
    return {
        "version": "reward-spec-v1",
        "admission": _contract(
            "input admitted", "accepted", 10, {"length": "length"},
            "accepted.hit && accepted.length > 0",
        ),
        "root": _contract(
            "length exceeds capacity", "oversized", 20,
            {"length": "length", "capacity": "capacity", "object": "dst"},
            "oversized.hit && oversized.length > oversized.capacity",
        ),
        "target": _contract(
            "copy consumes oversized request", "copy", 30,
            {"length": "length", "object": "dst"},
            "copy.hit && copy.time > root.oversized.time && "
            "copy.object == root.oversized.object && "
            "copy.length == root.oversized.length",
            observability="derived",
        ),
    }


def test_compile_and_evaluate_three_stages():
    spec = _spec()
    assert len(compile_checkpoints(spec)) == 3
    hits = [
        {"event_point": "admission.accepted", "timestamp": 1, "fields": {"length": 9}},
        {
            "event_point": "root.oversized", "timestamp": 2,
            "fields": {"length": 9, "capacity": 4, "object": 123},
        },
        {
            "event_point": "target.copy", "timestamp": 3,
            "fields": {"length": 9, "object": 123},
        },
    ]
    result = summarize_reward(spec, hits, 0)
    assert result["verified_stage"] == 3
    assert result["reward"]["target"]["status"] == "satisfied"
    assert result["target_runtime"]["triggered"] is False


def test_target_must_consume_same_root_witness():
    spec = _spec()
    hits = [
        {"event_point": "admission.accepted", "timestamp": 1, "fields": {"length": 9}},
        {
            "event_point": "root.oversized", "timestamp": 2,
            "fields": {"length": 9, "capacity": 4, "object": 123},
        },
        {
            "event_point": "target.copy", "timestamp": 3,
            "fields": {"length": 9, "object": 456},
        },
    ]
    result = summarize_reward(spec, hits, 0)
    assert result["verified_stage"] == 2
    assert result["reward"]["target"]["status"] == "not_satisfied"


def test_function_calls_in_captures_are_rejected():
    spec = _spec()
    spec["root"]["events"][0]["capture"][0]["expression"] = "strlen(data)"
    try:
        validate_spec(spec)
    except RewardSpecError as exc:
        assert "function-call capture" in str(exc)
    else:
        raise AssertionError("function-call capture was accepted")
