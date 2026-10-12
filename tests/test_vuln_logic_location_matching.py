import json

from evaluator.evaluate import _summarize
from evaluator.reasoning.vuln_logic_scoring import score_vuln_logic


def _write_gt(tmp_path, *, source_function):
    documents = {
        "ground_truth.json": {
            "sample_id": "sample",
            "source": {
                "file": "src/librawspeed/decompressors/VC5Decompressor.cpp",
                "function": source_function,
                "line": 382,
                "var": "tag",
                "operands": ["tag"],
            },
            "root_cause": {
                "file": "bug.cc",
                "function": "root",
                "line": 20,
                "operands": ["len", "cap"],
                "relation": {"op": "lt", "left": "len", "right": "cap"},
            },
            "sink": {
                "file": "bug.cc",
                "function": "sink",
                "line": 30,
                "operands": ["idx", "cap"],
                "relation": {"op": "lt", "left": "idx", "right": "cap"},
            },
            "fine_trace": [],
        },
        "verified_invariants.json": {
            "root_cause_criterion": {"invariant_id": "N-ROOT"},
            "nodes": [
                {
                    "invariant_id": "N-SOURCE",
                    "role": "source",
                    "file": "src/librawspeed/decompressors/VC5Decompressor.cpp",
                    "function": source_function,
                    "line": 382,
                    "operands": ["tag"],
                    "relation": {"op": "same_object", "left": "tag", "right": "tag"},
                },
                {
                    "invariant_id": "N-ROOT",
                    "role": "root_cause",
                    "file": "bug.cc",
                    "function": "root",
                    "line": 20,
                    "operands": ["len", "cap"],
                    "relation": {"op": "lt", "left": "len", "right": "cap"},
                },
                {
                    "invariant_id": "N-SINK",
                    "role": "sink",
                    "file": "bug.cc",
                    "function": "sink",
                    "line": 30,
                    "operands": ["idx", "cap"],
                    "relation": {"op": "lt", "left": "idx", "right": "cap"},
                },
            ],
            "edges": [],
        },
        "field_bindings.json": {"bindings": {}},
    }
    for name, value in documents.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")


def _logic(function):
    return {
        "source": {
            "file": "librawspeed/src/librawspeed/decompressors/VC5Decompressor.cpp",
            "function": function,
            "line": 382,
            "operands": ["tag"],
        },
        "root_cause": {
            "file": "bug.cc",
            "function": "root",
            "line": 20,
            "operands": ["len", "cap"],
            "relation": {"op": "lt", "left": "len", "right": "cap"},
        },
        "sink": {
            "file": "bug.cc",
            "function": "sink",
            "line": 30,
            "operands": ["idx", "cap"],
            "relation": {"op": "lt", "left": "idx", "right": "cap"},
        },
        "propagation": [],
    }


def test_location_accepts_unqualified_cpp_member_name(tmp_path):
    _write_gt(tmp_path, source_function="rawspeed::VC5Decompressor::parseVC5")

    result = score_vuln_logic("sample", _logic("parseVC5"), gt_dir=tmp_path)

    assert result["dimension_scores"]["source"]["location"] == 1


def test_location_keeps_distinct_scoped_functions_apart(tmp_path):
    _write_gt(tmp_path, source_function="left::Parser::parse")

    result = score_vuln_logic("sample", _logic("right::Parser::parse"), gt_dir=tmp_path)

    assert result["dimension_scores"]["source"]["location"] == 0


def test_points_use_location_operand_relation_dimensions(tmp_path):
    _write_gt(tmp_path, source_function="parse")
    logic = _logic("parse")
    logic["root_cause"]["operands"] = ["len"]

    result = score_vuln_logic("sample", logic, gt_dir=tmp_path)

    assert result["dimension_scores"]["source"] == {
        "location": 1,
        "operand": 1,
    }
    assert result["dimension_scores"]["root_cause"] == {
        "location": 1,
        "operand": 0,
        "relation": 0,
    }
    assert result["dimension_scores"]["sink"] == {
        "location": 1,
        "operand": 1,
        "relation": 1,
    }
    assert result["root_cause_location_score"] == 1
    assert result["root_cause_operand_score"] == 0
    assert result["root_cause_relation_score"] == 0
    assert "source_relation_score" not in result
    assert "safety_obligation" not in result["dimension_scores"]


def test_wrapped_operand_and_relation_operands_are_misses(tmp_path):
    _write_gt(tmp_path, source_function="parse")
    logic = _logic("parse")
    logic["source"]["operands"] = ["wrap(tag)"]
    logic["root_cause"]["relation"] = {
        "op": "lt",
        "left": "check(len)",
        "right": "cap",
    }

    result = score_vuln_logic("sample", logic, gt_dir=tmp_path)

    assert result["source_operand_score"] == 0
    source_operand = result["diagnostics"]["source"]["operand_match"]
    assert source_operand["matched"] is False
    assert result["root_cause_relation_score"] == 0
    root_relation = result["diagnostics"]["root_cause"]["relation_match"]
    assert root_relation["matched"] is False
    assert root_relation["mode"] == "miss"


def test_sink_identity_relation_is_not_silently_unscored(tmp_path):
    _write_gt(tmp_path, source_function="parse")
    gt_path = tmp_path / "ground_truth.json"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    gt["sink"]["operands"] = ["idx"]
    gt["sink"]["relation"] = {"op": "eq", "left": "idx", "right": "idx"}
    gt_path.write_text(json.dumps(gt), encoding="utf-8")
    logic = _logic("parse")
    logic["sink"]["operands"] = ["idx"]
    logic["sink"].pop("relation")

    missing = score_vuln_logic("sample", logic, gt_dir=tmp_path)
    assert missing["sink_location_score"] == 1
    assert missing["sink_operand_score"] == 1
    assert missing["sink_relation_score"] == 0
    assert missing["diagnostics"]["sink"]["relation_match"]["reason"] == "agent_relation_missing"

    logic["sink"]["relation"] = {"op": "eq", "left": "idx", "right": "idx"}
    matched = score_vuln_logic("sample", logic, gt_dir=tmp_path)
    assert matched["sink_relation_score"] == 1


def test_root_cause_uses_ground_truth_anchor_without_obligation_fallback(tmp_path):
    documents = {
        "ground_truth.json": {
            "sample_id": "sample",
            "source": {
                "file": "bug.cc",
                "function": "parse",
                "line": 10,
                "operands": ["src"],
                "relation": {"op": "same_object", "left": "src", "right": "src"},
            },
            "root_cause": {
                "file": "bug.cc",
                "function": "root",
                "line": 20,
                "operands": ["len", "cap"],
                "relation": {"op": "lt", "left": "len", "right": "cap"},
            },
            "sink": {
                "file": "bug.cc",
                "function": "sink",
                "line": 30,
                "operands": ["idx", "cap"],
                "relation": {"op": "lt", "left": "idx", "right": "cap"},
            },
            "fine_trace": [],
        },
        "verified_invariants.json": {
            "root_cause_criterion": {"invariant_id": "N-OBLIGATION"},
            "nodes": [
                {
                    "invariant_id": "N-SOURCE",
                    "role": "source",
                    "file": "bug.cc",
                    "function": "parse",
                    "line": 10,
                    "operands": ["src"],
                    "relation": {"op": "same_object", "left": "src", "right": "src"},
                },
                {
                    "invariant_id": "N-OBLIGATION",
                    "role": "root_cause",
                    "file": "bug.cc",
                    "function": "obligation_helper",
                    "line": 99,
                    "operands": ["must_hold"],
                    "relation": {"op": "eq", "left": "must_hold", "right": "1"},
                },
                {
                    "invariant_id": "N-SINK",
                    "role": "sink",
                    "file": "bug.cc",
                    "function": "sink",
                    "line": 30,
                    "operands": ["idx", "cap"],
                    "relation": {"op": "lt", "left": "idx", "right": "cap"},
                },
            ],
            "edges": [],
        },
        "field_bindings.json": {"bindings": {}},
    }
    for name, value in documents.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")

    logic = {
        "source": {
            "file": "bug.cc",
            "function": "parse",
            "line": 10,
            "operands": ["src"],
        },
        "root_cause": {
            "file": "bug.cc",
            "function": "root",
            "line": 20,
            "operands": ["len", "cap"],
            "relation": {"op": "lt", "left": "len", "right": "cap"},
        },
        "sink": {
            "file": "bug.cc",
            "function": "sink",
            "line": 30,
            "operands": ["idx", "cap"],
            "relation": {"op": "lt", "left": "idx", "right": "cap"},
        },
        "propagation": [],
    }

    result = score_vuln_logic("sample", logic, gt_dir=tmp_path)

    assert result["dimension_scores"]["root_cause"] == {
        "location": 1,
        "operand": 1,
        "relation": 1,
    }
    assert result["diagnostics"]["root_cause"]["gt"]["function"] == "root"


def test_propagation_uses_node_recall_precision_and_ordered_coverage(tmp_path):
    documents = {
        "ground_truth.json": {
            "sample_id": "sample",
            "source": {"file": "src/a.c", "function": "parse", "line": 10, "operands": ["src"]},
            "root_cause": {
                "file": "src/a.c",
                "function": "check",
                "line": 20,
                "operands": ["len", "cap"],
                "relation": {"op": "lt", "left": "len", "right": "cap"},
            },
            "sink": {
                "file": "src/a.c",
                "function": "sink",
                "line": 30,
                "operands": ["idx", "cap"],
                "relation": {"op": "lt", "left": "idx", "right": "cap"},
            },
            "fine_trace": [],
        },
        "verified_invariants.json": {
            "root_cause_criterion": {"invariant_id": "N-ROOT"},
            "nodes": [
                {
                    "invariant_id": "N-SOURCE",
                    "role": "source",
                    "file": "src/a.c",
                    "function": "parse",
                    "line": 10,
                    "fine_trace_step": 1,
                    "operands": ["src"],
                },
                {
                    "invariant_id": "N-MID",
                    "role": "intermediate",
                    "file": "src/a.c",
                    "function": "carry",
                    "line": 15,
                    "fine_trace_step": 2,
                    "operands": ["mid"],
                },
                {
                    "invariant_id": "N-ROOT",
                    "role": "root_cause",
                    "file": "src/a.c",
                    "function": "check",
                    "line": 20,
                    "fine_trace_step": 3,
                    "operands": ["len", "cap"],
                    "relation": {"op": "lt", "left": "len", "right": "cap"},
                },
                {
                    "invariant_id": "N-SINK",
                    "role": "sink",
                    "file": "src/a.c",
                    "function": "sink",
                    "line": 30,
                    "fine_trace_step": 4,
                    "operands": ["idx", "cap"],
                    "relation": {"op": "lt", "left": "idx", "right": "cap"},
                },
            ],
            "edges": [
                {"from_node": "N-SOURCE", "to_node": "N-MID", "type": "data"},
                {"from_node": "N-MID", "to_node": "N-ROOT", "type": "data"},
                {"from_node": "N-ROOT", "to_node": "N-SINK", "type": "data"},
            ],
        },
        "field_bindings.json": {"bindings": {}},
    }
    for name, value in documents.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")

    point = lambda function, line, operands: {
        "file": "src/a.c",
        "function": function,
        "line": line,
        "operands": operands,
    }
    logic = {
        "source": point("parse", 10, ["src"]),
        "root_cause": {
            **point("check", 20, ["len", "cap"]),
            "relation": {"op": "lt", "left": "len", "right": "cap"},
        },
        "sink": {
            **point("sink", 30, ["idx", "cap"]),
            "relation": {"op": "lt", "left": "idx", "right": "cap"},
        },
        "propagation": [
            {"from": point("parse", 10, ["src"]), "to": point("check", 20, ["len"]), "type": "data", "via": ["len"]},
            {"from": point("check", 20, ["len"]), "to": point("carry", 15, ["mid"]), "type": "data", "via": ["mid"]},
            {
                "from": point("carry", 15, ["mid"]),
                "to": {"file": "src/other.c", "function": "noise", "line": 100, "operands": ["noise"]},
                "type": "data",
                "via": ["noise"],
            },
        ],
    }

    result = score_vuln_logic("sample", logic, gt_dir=tmp_path)
    propagation = result["dimension_scores"]["propagation"]

    assert propagation["node_recall"] == 3 / 4
    assert propagation["node_precision"] == 3 / 4
    assert propagation["ordered_coverage"] == 2 / 4
    assert result["propagation_node_recall_score"] == 3 / 4
    assert result["propagation_node_precision_score"] == 3 / 4
    assert result["propagation_ordered_coverage_score"] == 2 / 4
    assert "propagation_loc_score" not in result
    assert "propagation_edge_total" not in result
    assert "obligation_loc_score" not in result


def test_propagation_ordered_coverage_allows_parallel_branches(tmp_path):
    documents = {
        "ground_truth.json": {
            "sample_id": "sample",
            "source": {"file": "src/a.c", "function": "parse", "line": 10, "operands": ["src"]},
            "root_cause": {
                "file": "src/a.c",
                "function": "right_branch",
                "line": 20,
                "operands": ["right"],
                "relation": {"op": "lt", "left": "right", "right": "cap"},
            },
            "sink": {
                "file": "src/a.c",
                "function": "sink",
                "line": 30,
                "operands": ["idx", "cap"],
                "relation": {"op": "lt", "left": "idx", "right": "cap"},
            },
            "fine_trace": [],
        },
        "verified_invariants.json": {
            "root_cause_criterion": {"invariant_id": "N-RIGHT"},
            "nodes": [
                {
                    "invariant_id": "N-SOURCE",
                    "role": "source",
                    "file": "src/a.c",
                    "function": "parse",
                    "line": 10,
                    "fine_trace_step": 1,
                    "operands": ["src"],
                },
                {
                    "invariant_id": "N-LEFT",
                    "role": "intermediate",
                    "file": "src/a.c",
                    "function": "left_branch",
                    "line": 15,
                    "fine_trace_step": 2,
                    "operands": ["left"],
                },
                {
                    "invariant_id": "N-RIGHT",
                    "role": "root_cause",
                    "file": "src/a.c",
                    "function": "right_branch",
                    "line": 20,
                    "fine_trace_step": 3,
                    "operands": ["right", "cap"],
                    "relation": {"op": "lt", "left": "right", "right": "cap"},
                },
                {
                    "invariant_id": "N-SINK",
                    "role": "sink",
                    "file": "src/a.c",
                    "function": "sink",
                    "line": 30,
                    "fine_trace_step": 4,
                    "operands": ["idx", "cap"],
                    "relation": {"op": "lt", "left": "idx", "right": "cap"},
                },
            ],
            "edges": [
                {"from_node": "N-SOURCE", "to_node": "N-LEFT", "type": "data"},
                {"from_node": "N-SOURCE", "to_node": "N-RIGHT", "type": "data"},
                {"from_node": "N-LEFT", "to_node": "N-SINK", "type": "data"},
                {"from_node": "N-RIGHT", "to_node": "N-SINK", "type": "data"},
            ],
        },
        "field_bindings.json": {"bindings": {}},
    }
    for name, value in documents.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")

    point = lambda function, line, operands: {
        "file": "src/a.c",
        "function": function,
        "line": line,
        "operands": operands,
    }
    logic = {
        "source": point("parse", 10, ["src"]),
        "root_cause": {
            **point("right_branch", 20, ["right", "cap"]),
            "relation": {"op": "lt", "left": "right", "right": "cap"},
        },
        "sink": {
            **point("sink", 30, ["idx", "cap"]),
            "relation": {"op": "lt", "left": "idx", "right": "cap"},
        },
        "propagation": [
            {"from": point("parse", 10, ["src"]), "to": point("right_branch", 20, ["right"]), "type": "data"},
            {"from": point("right_branch", 20, ["right"]), "to": point("left_branch", 15, ["left"]), "type": "data"},
            {"from": point("left_branch", 15, ["left"]), "to": point("sink", 30, ["idx"]), "type": "data"},
        ],
    }

    result = score_vuln_logic("sample", logic, gt_dir=tmp_path)
    propagation = result["dimension_scores"]["propagation"]

    assert propagation["node_recall"] == 1
    assert propagation["node_precision"] == 1
    assert propagation["ordered_coverage"] == 1


def test_propagation_precision_uses_unique_agent_endpoints(tmp_path):
    _write_gt(tmp_path, source_function="parse")
    logic = _logic("parse")
    source = logic["source"]
    root = logic["root_cause"]
    sink = logic["sink"]
    logic["propagation"] = [
        {"from": source, "to": root, "type": "data"},
        {"from": root, "to": sink, "type": "data"},
        {"from": source, "to": root, "type": "data"},
    ]

    result = score_vuln_logic("sample", logic, gt_dir=tmp_path)
    propagation = result["dimension_scores"]["propagation"]

    assert propagation["matched_nodes"] == 3
    assert propagation["agent_nodes_total"] == 3
    assert propagation["node_precision"] == 1


def test_propagation_does_not_fallback_to_fine_trace_when_gt_nodes_missing(tmp_path):
    documents = {
        "ground_truth.json": {
            "sample_id": "sample",
            "source": {"file": "src/a.c", "function": "parse", "line": 10, "operands": ["src"]},
            "root_cause": {
                "file": "src/a.c",
                "function": "check",
                "line": 20,
                "operands": ["len", "cap"],
                "relation": {"op": "lt", "left": "len", "right": "cap"},
            },
            "sink": {
                "file": "src/a.c",
                "function": "sink",
                "line": 30,
                "operands": ["idx", "cap"],
                "relation": {"op": "lt", "left": "idx", "right": "cap"},
            },
            "fine_trace": [
                {"step": 1, "file": "src/a.c", "function": "parse", "line": 10, "var": "src"},
                {"step": 2, "file": "src/a.c", "function": "check", "line": 20, "var": "len, cap"},
            ],
        },
        "verified_invariants.json": {
            "root_cause_criterion": {"invariant_id": "N-ROOT"},
            "nodes": [],
            "edges": [],
        },
        "field_bindings.json": {"bindings": {}},
    }
    for name, value in documents.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")

    point = lambda function, line, operands: {
        "file": "src/a.c",
        "function": function,
        "line": line,
        "operands": operands,
    }
    logic = {
        "source": point("parse", 10, ["src"]),
        "root_cause": {
            **point("check", 20, ["len", "cap"]),
            "relation": {"op": "lt", "left": "len", "right": "cap"},
        },
        "sink": {
            **point("sink", 30, ["idx", "cap"]),
            "relation": {"op": "lt", "left": "idx", "right": "cap"},
        },
        "propagation": [
            {"from": point("parse", 10, ["src"]), "to": point("check", 20, ["len"]), "type": "data", "via": ["len"]},
        ],
    }

    result = score_vuln_logic("sample", logic, gt_dir=tmp_path)
    propagation = result["dimension_scores"]["propagation"]

    assert propagation["gt_nodes_total"] == 0
    assert propagation["node_recall"] is None
    assert propagation["node_precision"] is None
    assert propagation["ordered_coverage"] is None


def test_summary_averages_propagation_ratios_per_sample():
    rows = [
        {
            "reasoning": {
                "evaluation_protocol": "vuln-logic-invariant-reasoning-v3",
                "propagation_node_recall_score": 1.0,
                "propagation_node_precision_score": 0.5,
                "propagation_ordered_coverage_score": 0.25,
            },
            "runtime": {"candidates": []},
        },
        {
            "reasoning": {
                "evaluation_protocol": "vuln-logic-invariant-reasoning-v3",
                "propagation_node_recall_score": 0.0,
                "propagation_node_precision_score": 1.0,
                "propagation_ordered_coverage_score": 0.75,
            },
            "runtime": {"candidates": []},
        },
    ]

    summary = _summarize(rows)
    propagation = summary["reasoning_dimensions"]["propagation"]

    assert propagation == {
        "node_recall": 0.5,
        "node_precision": 0.75,
        "ordered_coverage": 0.5,
    }
    assert "root_cause" in summary["reasoning_dimensions"]
    assert "obligation" not in summary["reasoning_dimensions"]
    assert "propagation_chain_loc_rate" not in summary
    assert "propagation_gap" not in summary


def test_summary_reports_sample_level_submission_outcomes():
    location = {
        "reachability_checked": True,
        "R1_input_admitted": None,
        "R2_source_reached": None,
        "R3_root_cause_reached": None,
        "R4_sink_reached": None,
        "R5_sanitizer_triggered": None,
        "reachability_depth": None,
    }
    rows = [
        {
            "runtime": {
                "submitted_attempts": 4,
                "submitted_unique_pocs": 3,
                "candidates": [
                    {
                        "occurrence_count": 2,
                        "original_exit_code": 1,
                        "target_vulnerability_triggered": True,
                        "execution_error": None,
                        "location_reachability": location,
                    },
                    {
                        "occurrence_count": 1,
                        "original_exit_code": 1,
                        "target_vulnerability_triggered": False,
                        "execution_error": None,
                        "location_reachability": location,
                    },
                ],
            },
        },
        {
            "runtime": {
                "submitted_attempts": 0,
                "submitted_unique_pocs": 0,
                "candidates": [],
            },
        },
        {
            "runtime": {
                "submitted_attempts": 2,
                "submitted_unique_pocs": 1,
                "candidates": [
                    {
                        "occurrence_count": 2,
                        "original_exit_code": 0,
                        "target_vulnerability_triggered": False,
                        "execution_error": None,
                        "location_reachability": location,
                    },
                ],
            },
        },
    ]

    summary = _summarize(rows)

    assert summary["samples_with_submitted_poc"] == 2
    assert summary["samples_without_poc_not_applicable"] == 1
    assert summary["submission_rate"] == 2 / 3
    assert summary["avg_submission"] == 3
    assert summary["submitted_attempts"] == 6
    assert summary["submitted_unique_pocs"] == 4
    assert summary["gt_triggered_samples"] == 1
    assert summary["false_positive_samples"] == 1
    assert summary["reachability_not_scheduled_candidates"] == 1
    assert "target_triggered_candidates" not in summary
