import json

from evaluator.compiled_graph import compile_invariant_graph
from evaluator.evaluate import _summarize
from evaluator.reasoning.fine_trace import validate_fine_trace
from evaluator.reasoning.invariant_scoring import score_compiled_trace
from evaluator.reachability.state_scoring import score_runtime_events


def _write_gt(tmp_path):
    fine_trace = _trace()
    documents = {
        "ground_truth.json": {
            "sample_id": "sample",
            "source": {"file": "p.c", "function": "parse", "line": 10, "var": "input", "trace_step": 1},
            "root_cause": {"file": "p.c", "function": "parse", "line": 20, "var": "length, buffer_capacity", "trace_step": 2},
            "sink": {"file": "p.c", "function": "sink", "line": 30},
            "reachability_checkpoints": {
                "parser_admitted": {"file": "p.c", "function": "parse", "line": 9}
            },
            "fine_trace": fine_trace,
        },
        "verified_assertions.json": {"assertions": [
            {"id": "root.bound", "invariants": ["root"], "kind": "required", "at": "decision", "protects": "read", "check": ["lt", "$decision.length", "$decision.capacity"]},
            {"id": "edge.carry", "invariants": ["edge"], "kind": "transition", "from": "decision", "at": "read", "check": ["eq", "$decision.length", "$read.index"]},
        ]},
        "verified_invariants.json": {
            "root_cause_criterion": {"invariant_id": "root", "file": "p.c", "function": "parse", "line": 20, "fine_trace_step": 2},
            "nodes": [],
            "edges": [{"invariant_id": "edge", "from_file": "p.c", "from_function": "parse", "from_line": 20, "from_step": 2, "to_file": "p.c", "to_function": "sink", "to_line": 30, "to_step": 3}],
        },
        "assertion_results.json": {"original_case": "original", "assertions": [
            {"id": "root.bound", "matrix": {"vulnerable": {"original": {"satisfied": False, "status": "violated"}}}},
            {"id": "edge.carry", "matrix": {"vulnerable": {"original": {"satisfied": True, "status": "satisfied"}}}},
        ]},
        "field_bindings.json": {"bindings": {"decision.length": "length", "decision.capacity": "buffer_capacity", "read.index": "index"}},
        "event_locations.json": {"locations": {
            "decision": {"file": "p.c", "function": "parse", "line": 20},
            "read": {"file": "p.c", "function": "sink", "line": 30},
        }},
    }
    for name, value in documents.items():
        (tmp_path / name).write_text(json.dumps(value))


def _trace():
    return [
        {"step": 1, "file": "p.c", "function": "parse", "line": 10, "var": "input", "code": "length = read(input);", "note": "input"},
        {"step": 2, "file": "p.c", "function": "parse", "line": 20, "var": "length, buffer_capacity", "code": "if (length < buffer_capacity)", "note": "root"},
        {"step": 3, "file": "p.c", "function": "sink", "line": 30, "var": "index", "code": "buffer[index]", "note": "sink"},
    ]


def test_compiler_builds_one_reasoning_and_runtime_contract(tmp_path):
    _write_gt(tmp_path)
    graph = compile_invariant_graph(tmp_path)
    assert graph.errors == ()
    assert graph.admission.line == 9
    assert len(graph.edges) == 1
    assert len(graph.runtime.root_conditions) == 1


def test_reasoning_requires_coherent_operands_and_order(tmp_path):
    _write_gt(tmp_path)
    graph = compile_invariant_graph(tmp_path)
    correct = score_compiled_trace(graph, _trace())
    missing_operand = _trace()
    missing_operand[1]["var"] = "length"
    missing_operand[1]["code"] = "use(length);"
    wrong_root = score_compiled_trace(graph, missing_operand)
    reversed_trace = [_trace()[0], _trace()[2], _trace()[1]]
    reversed = score_compiled_trace(graph, reversed_trace)
    assert correct["source_score"] == 1
    assert correct["root_cause_score"] == 1
    assert correct["propagation_score"] == 1
    assert wrong_root["root_cause_score"] == 0
    assert reversed["propagation_score"] == 0


def test_statement_range_accepts_overlap_but_not_nearby_line(tmp_path):
    _write_gt(tmp_path)
    gt_path = tmp_path / "ground_truth.json"
    gt = json.loads(gt_path.read_text())
    gt["fine_trace"][1]["line_end"] = 21
    gt_path.write_text(json.dumps(gt))
    graph = compile_invariant_graph(tmp_path)

    overlapping = _trace()
    overlapping[1]["line"] = 21
    accepted = score_compiled_trace(graph, overlapping)
    assert accepted["root_cause_score"] == 1
    assert accepted["propagation_score"] == 1

    nearby = _trace()
    nearby[1]["line"] = 22
    rejected = score_compiled_trace(graph, nearby)
    assert rejected["root_cause_score"] == 0
    assert rejected["propagation_score"] == 0


def test_reasoning_never_uses_free_form_note(tmp_path):
    _write_gt(tmp_path)
    graph = compile_invariant_graph(tmp_path)
    trace = _trace()
    baseline = score_compiled_trace(graph, trace)
    for step in trace:
        step["note"] = "completely contradictory arbitrary prose"
    changed = score_compiled_trace(graph, trace)
    assert baseline["source_score"] == changed["source_score"]
    assert baseline["root_cause_score"] == changed["root_cause_score"]
    assert baseline["propagation_score"] == changed["propagation_score"]


def test_runtime_uses_strict_prefix_and_runtime_values(tmp_path):
    _write_gt(tmp_path)
    safe_root = score_runtime_events(
        tmp_path,
        [{"point": "decision", "fields": {"length": 2, "capacity": 8}, "order": 0}, {"point": "read", "fields": {"index": 2}, "order": 1}],
        parser_admitted=True, source_reached=True, target_triggered=True,
    )
    assert safe_root["R3_root_event_reached"] is True
    assert safe_root["R3_root_conditions_matched"] is False
    assert safe_root["reachability_depth"] == "R2"
    assert safe_root["target_vulnerability_triggered"] is True

    vulnerable = score_runtime_events(
        tmp_path,
        [{"point": "decision", "fields": {"length": 8, "capacity": 8}, "order": 0}, {"point": "read", "fields": {"index": 8}, "order": 1}],
        parser_admitted=True, source_reached=True, target_triggered=False,
    )
    assert vulnerable["R3_root_conditions_matched"] is True
    assert vulnerable["R4_causal_graph_matched"] is True
    assert vulnerable["reachability_depth"] == "R4"

    not_admitted = score_runtime_events(
        tmp_path,
        [{"point": "decision", "fields": {"length": 8, "capacity": 8}, "order": 0}, {"point": "read", "fields": {"index": 8}, "order": 1}],
        parser_admitted=False, source_reached=True, target_triggered=True,
    )
    assert not_admitted["R4_causal_graph_matched"] is False
    assert not_admitted["reachability_depth"] == "R0"


def test_trace_validator_rejects_depends_on():
    trace = _trace()
    assert validate_fine_trace(json.dumps(trace)) is None
    trace[1]["depends_on"] = [{"on": 1}]
    assert "must not contain depends_on" in validate_fine_trace(json.dumps(trace))


def test_summary_separates_no_poc_unexecuted_and_runtime_unavailable():
    rows = [
        {
            "reasoning": {},
            "runtime": {"submitted_unique_pocs": 0, "candidates": []},
        },
        {
            "reasoning": {},
            "runtime": {
                "submitted_unique_pocs": 2,
                "candidates": [{
                    "target_vulnerability_triggered": False,
                    "location_reachability": {
                        "reachability_checked": True,
                        "R1_input_admitted": True,
                        "R2_source_reached": True,
                        "R3_root_cause_reached": False,
                        "R4_sink_reached": False,
                        "reachability_depth": "R2",
                    },
                }],
            },
        },
    ]
    summary = _summarize(rows)
    assert summary["submitted_unique_pocs"] == 2
    assert summary["reachability_executed_candidates"] == 1
    assert summary["reachability_unavailable_candidates"] == 1
    assert summary["reachability_depth_distribution"]["R2"] == 1
    assert summary["R3_evaluable_candidates"] == 1
    assert summary["R3_reach_rate"] == 0
