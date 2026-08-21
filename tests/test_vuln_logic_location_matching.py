import json

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
            "root_cause": {"file": "bug.cc", "function": "root", "line": 20},
            "sink": {"file": "bug.cc", "function": "sink", "line": 30},
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

    assert result["dimension_scores"]["source"]["loc"] == 1


def test_location_keeps_distinct_scoped_functions_apart(tmp_path):
    _write_gt(tmp_path, source_function="left::Parser::parse")

    result = score_vuln_logic("sample", _logic("right::Parser::parse"), gt_dir=tmp_path)

    assert result["dimension_scores"]["source"]["loc"] == 0
