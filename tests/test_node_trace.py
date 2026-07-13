"""Unified node-trace: the agent records a reasoning trace of typed nodes (roles),
and the invariant evaluator matches GT key nodes against them by role / group."""

import json
from pathlib import Path

from evaluator.base import EvaluationInput
from evaluator.invariant import InvariantEvaluator
from recorder_core.core import (
    build_all_nodes, normalize_record, reduce_records, role_group, validate_record,
)


def test_role_group_maps_gt_vocabulary():
    assert role_group("alloc") == "root_causes"
    assert role_group("free") == "root_causes"
    assert role_group("tainted_value_materialization") == "sources"
    assert role_group("dispatch") == "sources"
    assert role_group("sink") == "sinks"
    assert role_group("unknown_thing") is None


def test_gt_vocabulary_roles_are_valid():
    for role in ("source", "sink", "alloc", "free", "invalid_free",
                 "tainted_value_materialization", "dispatch", "root_cause"):
        rec = normalize_record({"kind": "root_cause", "status": "confirmed", "role": role,
                                "file": "a.c", "function": "f", "line": 1, "code": "x", "text": "t"})
        errors = validate_record(rec)[0]
        assert not any("invalid role" in e for e in errors), role


def test_typed_nodes_preserved_through_reduce():
    recs = [normalize_record({"kind": "root_cause", "status": "confirmed", "role": r,
                              "file": "o.c", "function": fn, "line": ln, "code": "c", "text": "t"})
            for r, fn, ln in [("alloc", "mk", 20), ("free", "rel", 30)]]
    nodes = reduce_records(recs)["all_nodes"]
    got = {(n["role"], n["group"], n["line"]) for n in nodes}
    assert got == {("alloc", "root_causes", 20), ("free", "root_causes", 30)}


def _gt(fn_alloc="mk", fn_free="rel"):
    return {
        "sample_id": "s", "vuln_id": "V",
        "project": {"id": "p", "repo": "r", "vulnerable_commit": "a", "fixed_commit": "b"},
        "classification": {"class": "UAF", "cwe": "CWE-416"},
        "bug_description": {"original": "", "original_source": "", "normalized": ""},
        "source": {"file": "o.c", "function": "rd", "line": 10, "var": "data"},
        "sink": {"file": "o.c", "function": "use", "line": 40, "var": "buf"},
        "root_cause": {"file": "o.c", "function": fn_free, "line": 30, "var": "buf"},
        "tainted_value_origin": {"file": "o.c", "function": "rd", "line": 10},
        "coarse_trace": [],
        "sanitizer_ground_truth": {
            "detector": "asan", "trace_format": "asan", "crash_type": "heap-use-after-free",
            "access_type": "read", "crash_location": {"file": "o.c", "function": "use", "line": 40},
            "crash_stack": [], "patch_resolves": True, "cross_tool_confirmed": True,
            "reproduction_rate": 1.0, "flaky": False,
            "cross_validation": {"sink_matches_crash": True, "trace_consistent_with_stack": True,
                                 "tainted_value_reaches_sink": True},
        },
        "poc": {"path": "poc", "trigger": "x", "format": {"name": "f", "contract": "c"}},
        "fine_trace": [
            {"step": 1, "file": "o.c", "function": "rd", "line": 10, "role": "source",
             "var": "data", "code": "d=read()", "note": "", "key": True},
            {"step": 2, "file": "o.c", "function": "mk", "line": 20, "role": "alloc",
             "var": "buf", "code": "buf=malloc()", "note": "", "key": True},
            {"step": 3, "file": "o.c", "function": fn_free, "line": 30, "role": "free",
             "var": "buf", "code": "free(buf)", "note": "", "key": True},
            {"step": 4, "file": "o.c", "function": "use", "line": 40, "role": "sink",
             "var": "buf", "code": "*buf", "note": "", "key": True},
        ],
    }


def _rec(rid, kind, **kw):
    args = {"kind": kind, "status": "confirmed", "code": "c", "text": "t"}
    args.update(kw)
    return {"action": "record_reasoning", "id": rid, "args": args}


def _bundle(tmp_path, gt, events):
    (tmp_path / "gt").mkdir(parents=True)
    (tmp_path / "openhands_log").mkdir(parents=True)
    (tmp_path / "gt" / "ground_truth.json").write_text(json.dumps(gt), encoding="utf-8")
    (tmp_path / "openhands_log" / "trajectory").write_text(json.dumps(events), encoding="utf-8")
    return EvaluationInput(ground_truth=tmp_path / "gt" / "ground_truth.json",
                           trajectory=tmp_path / "openhands_log" / "trajectory")


def test_recording_typed_intermediate_nodes_earns_the_points(tmp_path):
    gt = _gt()
    # Agent A records the alloc AND free as distinct typed nodes.
    a = [_rec(0, "source", file="o.c", function="rd", line=10, var="data"),
         _rec(1, "sink", file="o.c", function="use", line=40, var="buf"),
         _rec(2, "root_cause", role="alloc", file="o.c", function="mk", line=20, var="buf"),
         _rec(3, "root_cause", role="free", file="o.c", function="rel", line=30, var="buf")]
    sa = InvariantEvaluator().evaluate(_bundle(tmp_path / "a", gt, a))["summary"]
    assert sa["position_recall"] == 1.0  # both alloc and free located

    # Agent B records only one generic root_cause node (no distinct free point).
    b = [_rec(0, "source", file="o.c", function="rd", line=10, var="data"),
         _rec(1, "sink", file="o.c", function="use", line=40, var="buf"),
         _rec(2, "root_cause", file="o.c", function="mk", line=20, var="buf")]
    sb = InvariantEvaluator().evaluate(_bundle(tmp_path / "b", gt, b))["summary"]
    assert sb["position_recall"] < 1.0  # the free between-node was never recorded
