"""Endpoints (source/sink) and invariant (between-reasoning) evaluators.

The two are deliberately separate signals:
  * endpoints scores localization of the two artifact-grounded anchors;
  * invariant scores the reasoning BETWEEN them (between-node position + typed
    edges), including the why-crash edge into the sink.
"""

import json
from pathlib import Path

from evaluator.base import EvaluationInput
from evaluator.invariant import InvariantEvaluator
from evaluator.t1_source_sink import T1SourceSinkEvaluator

GT = {
    "sample_id": "syn_uaf",
    "vuln_id": "SYN-1",
    "project": {"id": "p", "repo": "r", "vulnerable_commit": "a", "fixed_commit": "b"},
    "classification": {"class": "UAF", "cwe": "CWE-416"},
    "bug_description": {"original": "", "original_source": "", "normalized": ""},
    "source": {"file": "parse.c", "function": "parse", "line": 10},
    "sink": {"file": "obj.c", "function": "use", "line": 40},
    "root_cause": {"file": "obj.c", "function": "release", "line": 30},
    "tainted_value_origin": {"file": "parse.c", "function": "parse", "line": 10},
    "coarse_trace": [],
    "sanitizer_ground_truth": {
        "detector": "asan", "trace_format": "asan", "crash_type": "heap-use-after-free",
        "access_type": "read", "crash_location": {"file": "obj.c", "function": "use", "line": 40},
        "crash_stack": [], "patch_resolves": True, "cross_tool_confirmed": True,
        "reproduction_rate": 1.0, "flaky": False,
        "cross_validation": {"sink_matches_crash": True, "trace_consistent_with_stack": True,
                             "tainted_value_reaches_sink": True},
    },
    "poc": {"path": "poc", "trigger": "x", "format": {"name": "f", "contract": "c"}},
    "fine_trace": [
        {"step": 1, "file": "parse.c", "function": "parse", "line": 10, "role": "source",
         "var": "data", "code": "data = read()", "note": "", "key": True},
        {"step": 2, "file": "obj.c", "function": "make", "line": 20, "role": "alloc",
         "var": "buf", "code": "buf = malloc()", "note": "", "key": True,
         "depends_on": [{"on": 1, "type": "data", "via": "data"}]},
        {"step": 3, "file": "obj.c", "function": "release", "line": 30, "role": "free",
         "var": "buf", "code": "free(buf)", "note": "", "key": True,
         "depends_on": [{"on": 2, "type": "data", "via": "buf"}]},
        {"step": 4, "file": "obj.c", "function": "use", "line": 40, "role": "sink",
         "var": "buf", "code": "*buf", "note": "", "key": True,
         "depends_on": [{"on": 3, "type": "order", "via": "free_before_use", "obj": "buf"},
                        {"on": 2, "type": "data", "via": "buf"}]},
    ],
}


def _rec(rid, kind, **kw):
    args = {"kind": kind, "status": "confirmed", "code": "c", "text": "t"}
    args.update(kw)
    return {"action": "record_reasoning", "id": rid, "args": args}


def _claims(include_sink_order: bool):
    events = [
        _rec(0, "source", file="parse.c", function="parse", line=10, var="data"),
        _rec(1, "sink", file="obj.c", function="use", line=40, var="buf"),
        _rec(2, "root_cause", file="obj.c", function="make", line=20, var="buf"),
        _rec(3, "root_cause", file="obj.c", function="release", line=30, var="buf"),
        _rec(4, "edge", file="obj.c", function="make", line=20, type="data",
             relation="propagates", **{"from": "data", "to": "buf"}),
        _rec(5, "edge", file="obj.c", function="release", line=30, type="data",
             relation="propagates", **{"from": "buf", "to": "buf"}),
    ]
    if include_sink_order:
        events.append(_rec(6, "edge", file="obj.c", function="use", line=40, type="order",
                           relation="free_before_use", **{"from": "buf", "to": "buf"}))
    return events


def _bundle(tmp_path: Path, include_sink_order: bool) -> EvaluationInput:
    (tmp_path / "gt").mkdir()
    (tmp_path / "openhands_log").mkdir()
    (tmp_path / "gt" / "ground_truth.json").write_text(json.dumps(GT), encoding="utf-8")
    (tmp_path / "openhands_log" / "trajectory").write_text(
        json.dumps(_claims(include_sink_order)), encoding="utf-8")
    return EvaluationInput(ground_truth=tmp_path / "gt" / "ground_truth.json",
                           trajectory=tmp_path / "openhands_log" / "trajectory")


def test_endpoints_scored_independently(tmp_path: Path) -> None:
    inp = _bundle(tmp_path, include_sink_order=True)
    summary = T1SourceSinkEvaluator().evaluate(inp)["summary"]
    assert summary["source_status"] == "located"
    assert summary["sink_status"] == "located"
    assert summary["strict_source_sink_identified"] is True


def test_full_chain_is_fully_reasoned(tmp_path: Path) -> None:
    inp = _bundle(tmp_path, include_sink_order=True)
    summary = InvariantEvaluator().evaluate(inp)["summary"]
    assert summary["reasoning_recall"] == 1.0
    assert summary["edge_recall_by_type"]["order"] == 1.0
    assert summary["edge_recall_by_type"]["data"] == 1.0


def test_missing_why_crash_edge_penalizes_reasoning_not_just_edges(tmp_path: Path) -> None:
    """Dropping the free-before-use edge into the sink must lower reasoning_recall,
    not merely edge recall — otherwise the sink is a scored single point."""
    inp = _bundle(tmp_path, include_sink_order=False)
    summary = InvariantEvaluator().evaluate(inp)["summary"]
    # Between-nodes (alloc, free) still located and connected -> position floor holds.
    assert summary["position_recall"] == 1.0
    # But the sink edge unit is not reasoned -> reasoning_recall drops below the floor.
    assert summary["reasoning_recall"] < 1.0
    assert summary["edge_recall_by_type"]["order"] == 0.0


OOB_GT = {
    "sample_id": "syn_oob", "vuln_id": "SYN-2",
    "project": {"id": "p", "repo": "r", "vulnerable_commit": "a", "fixed_commit": "b"},
    "classification": {"class": "OOB", "cwe": "CWE-787"},
    "bug_description": {"original": "", "original_source": "", "normalized": ""},
    "source": {"file": "tga.c", "function": "read_header", "line": 88},
    "sink": {"file": "tga.c", "function": "decode_rle", "line": 214},
    "root_cause": {"file": "tga.c", "function": "decode_rle", "line": 201},
    "tainted_value_origin": {"file": "tga.c", "function": "read_header", "line": 88},
    "coarse_trace": [],
    "sanitizer_ground_truth": {
        "detector": "asan", "trace_format": "asan", "crash_type": "heap-buffer-overflow",
        "access_type": "write", "crash_location": {"file": "tga.c", "function": "decode_rle", "line": 214},
        "crash_stack": [], "patch_resolves": True, "cross_tool_confirmed": True,
        "reproduction_rate": 1.0, "flaky": False,
        "cross_validation": {"sink_matches_crash": True, "trace_consistent_with_stack": True,
                             "tainted_value_reaches_sink": True},
    },
    "poc": {"path": "poc", "trigger": "x", "format": {"name": "TGA", "contract": "c"}},
    "fine_trace": [
        {"step": 1, "file": "tga.c", "function": "read_header", "line": 88, "role": "source",
         "var": "width", "code": "width = read_le16(f)", "note": "", "key": True},
        {"step": 2, "file": "tga.c", "function": "decode_rle", "line": 201, "role": "root_cause",
         "var": "count", "code": "count = (packet & 0x7f) + 1", "note": "missing bounds check on out+count",
         "key": True,
         "depends_on": [{"on": 1, "type": "control", "via": "out + count > end"}]},
        {"step": 3, "file": "tga.c", "function": "decode_rle", "line": 214, "role": "sink",
         "var": "out", "code": "out[i] = pixel", "note": "", "key": True,
         "depends_on": [{"on": 2, "type": "control", "via": "out + count > end"}]},
    ],
}


def _oob_claims(include_control: bool):
    events = [
        _rec(0, "source", file="tga.c", function="read_header", line=88, var="width"),
        _rec(1, "sink", file="tga.c", function="decode_rle", line=214, var="out"),
        _rec(2, "root_cause", file="tga.c", function="decode_rle", line=201, var="count"),
    ]
    if include_control:
        events.append(_rec(3, "edge", file="tga.c", function="decode_rle", line=201, type="control",
                           relation="missing_bounds_check", obj="out", **{"from": "count", "to": "out"}))
        events.append(_rec(4, "edge", file="tga.c", function="decode_rle", line=214, type="control",
                           relation="unchecked", obj="out", **{"from": "count", "to": "out"}))
    return events


def _oob_bundle(tmp_path: Path, include_control: bool) -> EvaluationInput:
    (tmp_path / "gt").mkdir(parents=True)
    (tmp_path / "openhands_log").mkdir(parents=True)
    (tmp_path / "gt" / "ground_truth.json").write_text(json.dumps(OOB_GT), encoding="utf-8")
    (tmp_path / "openhands_log" / "trajectory").write_text(
        json.dumps(_oob_claims(include_control)), encoding="utf-8")
    return EvaluationInput(ground_truth=tmp_path / "gt" / "ground_truth.json",
                           trajectory=tmp_path / "openhands_log" / "trajectory")


def test_control_edge_matches_on_operands_not_prose(tmp_path: Path) -> None:
    """A control edge with a keyword `via` matches on guarded operands (out/count),
    and dropping it lowers reasoning_recall — the missing-check reasoning is scored."""
    full = InvariantEvaluator().evaluate(_oob_bundle(tmp_path / "a", include_control=True))["summary"]
    assert full["reasoning_recall"] == 1.0
    assert full["edge_recall_by_type"]["control"] == 1.0

    no_ctrl = InvariantEvaluator().evaluate(_oob_bundle(tmp_path / "b", include_control=False))["summary"]
    assert no_ctrl["position_recall"] == 1.0  # root_cause still located
    assert no_ctrl["reasoning_recall"] < 1.0  # ...but the missing check is not connected
    assert no_ctrl["edge_recall_by_type"]["control"] == 0.0


def test_no_key_marks_yields_no_reasoning_points(tmp_path: Path) -> None:
    gt = json.loads(json.dumps(GT))
    for step in gt["fine_trace"]:
        step.pop("key", None)
    (tmp_path / "gt").mkdir()
    (tmp_path / "openhands_log").mkdir()
    (tmp_path / "gt" / "ground_truth.json").write_text(json.dumps(gt), encoding="utf-8")
    (tmp_path / "openhands_log" / "trajectory").write_text(
        json.dumps(_claims(True)), encoding="utf-8")
    inp = EvaluationInput(ground_truth=tmp_path / "gt" / "ground_truth.json",
                          trajectory=tmp_path / "openhands_log" / "trajectory")
    summary = InvariantEvaluator().evaluate(inp)["summary"]
    assert summary["reasoning_points"] == 0
    assert summary["reasoning_recall"] is None


def _spray_events():
    """Agent records the right sink plus a pile of invented edges/nodes."""
    ev = [
        _rec(0, "source", file="tga.c", function="read_header", line=88, var="width"),
        _rec(1, "sink", file="tga.c", function="decode_rle", line=214, var="out"),
        _rec(2, "root_cause", file="tga.c", function="decode_rle", line=201, var="count"),
        _rec(3, "edge", file="tga.c", function="decode_rle", line=201, type="control",
             relation="x", **{"from": "count", "to": "out"}),
    ]
    # 6 invented data edges over unrelated variables
    for i, (a, b) in enumerate([("zz", "qq"), ("aa", "bb"), ("cc", "dd"),
                                ("ee", "ff"), ("gg", "hh"), ("ii", "jj")], start=4):
        ev.append(_rec(i, "edge", file="tga.c", function="decode_rle", line=200, type="data",
                       relation="p", **{"from": a, "to": b}))
    return ev


def test_precision_penalizes_spray_and_pray(tmp_path):
    (tmp_path / "gt").mkdir(parents=True); (tmp_path / "openhands_log").mkdir(parents=True)
    (tmp_path / "gt" / "ground_truth.json").write_text(json.dumps(OOB_GT), encoding="utf-8")
    (tmp_path / "openhands_log" / "trajectory").write_text(json.dumps(_spray_events()), encoding="utf-8")
    inp = EvaluationInput(ground_truth=tmp_path / "gt" / "ground_truth.json",
                          trajectory=tmp_path / "openhands_log" / "trajectory")
    s = InvariantEvaluator().evaluate(inp)["summary"]
    # It found the real control edge (recall ok) but drowned it in junk -> low precision.
    assert s["edge_recall"] and s["edge_recall"] > 0.0
    assert s["edge_precision"] is not None and s["edge_precision"] < 0.4
    assert s["edge_f1"] is not None and s["edge_f1"] < s["edge_recall"]
