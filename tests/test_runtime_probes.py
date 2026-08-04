import json

from evaluator.condition_graph import (
    Condition,
    ConditionGraph,
    Location,
    Operand,
)
from evaluator.reachability.probes import compile_runtime_probes, load_runtime_probes


def _graph():
    left = Operand(
        raw="$decision.length",
        event="decision",
        field="length",
        source_expression="length",
    )
    right = Operand(
        raw="$decision.capacity",
        event="decision",
        field="capacity",
        source_expression="capacity",
    )
    return ConditionGraph(
        sample_id="sample",
        source=Location("p.c", "parse", 1),
        root=Location("p.c", "parse", 2),
        sink=Location("p.c", "sink", 3),
        event_locations={"decision": Location("p.c", "parse", 2)},
        conditions=(
            Condition(
                assertion_id="root",
                invariant_ids=("root",),
                kind="required",
                operator="lt",
                left=left,
                right=right,
                at="decision",
                source_event=None,
                protects="read",
                expected_satisfied=False,
                expected_status="violated",
            ),
        ),
        errors=(),
    )


def test_runtime_probe_contract_requires_every_condition_field(tmp_path):
    path = tmp_path / "runtime_probes.json"
    path.write_text(json.dumps({
        "events": {
            "decision": {
                "captures": {"length": "(long) length"}
            }
        }
    }))
    checkpoints, errors = load_runtime_probes(path, _graph())
    assert checkpoints[0]["kind"] == "condition_event"
    assert checkpoints[0]["file"] == "p.c"
    assert errors == ["runtime probe decision missing fields: capacity"]


def test_runtime_probes_are_compiled_ephemerally_from_bindings():
    checkpoints, errors = compile_runtime_probes(_graph())
    assert errors == []
    assert checkpoints == [{
        "kind": "condition_event",
        "event_point": "decision",
        "file": "p.c",
        "function": "parse",
        "line": 2,
        "captures": {"length": "length", "capacity": "capacity"},
    }]
