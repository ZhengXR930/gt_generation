"""Structured T3: the cause-vs-symptom distinction + causal link."""

import json
from pathlib import Path

from evaluator.base import EvaluationInput
from evaluator.t3_root_cause import T3RootCauseEvaluator


def _base_gt():
    return {
        "sample_id": "syn", "vuln_id": "SYN",
        "project": {"id": "p", "repo": "r", "vulnerable_commit": "a", "fixed_commit": "b"},
        "classification": {"class": "OOB", "cwe": "CWE-787"},
        "bug_description": {"original": "", "original_source": "", "normalized": ""},
        "source": {"file": "tga.c", "function": "read_header", "line": 88},
        "sink": {"file": "tga.c", "function": "decode_rle", "line": 214, "var": "out"},
        "root_cause": {"file": "tga.c", "function": "decode_rle", "line": 201, "var": "count"},
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
        "fine_trace": [],
    }


def _rec(rid, kind, **kw):
    args = {"kind": kind, "status": "confirmed", "code": "c", "text": "t"}
    args.update(kw)
    return {"action": "record_reasoning", "id": rid, "args": args}


def _bundle(tmp_path: Path, gt: dict, events: list) -> EvaluationInput:
    (tmp_path / "gt").mkdir(parents=True)
    (tmp_path / "openhands_log").mkdir(parents=True)
    (tmp_path / "gt" / "ground_truth.json").write_text(json.dumps(gt), encoding="utf-8")
    (tmp_path / "openhands_log" / "trajectory").write_text(json.dumps(events), encoding="utf-8")
    return EvaluationInput(ground_truth=tmp_path / "gt" / "ground_truth.json",
                           trajectory=tmp_path / "openhands_log" / "trajectory")


def test_true_cause_located_and_linked(tmp_path: Path) -> None:
    gt = _base_gt()
    events = [
        _rec(0, "root_cause", file="tga.c", function="decode_rle", line=201, var="count"),
        _rec(1, "sink", file="tga.c", function="decode_rle", line=214, var="out"),
        _rec(2, "edge", file="tga.c", function="decode_rle", line=201, type="control",
             relation="missing_check", **{"from": "count", "to": "out"}),
    ]
    s = T3RootCauseEvaluator().evaluate(_bundle(tmp_path, gt, events))["summary"]
    assert s["distinguishes_cause_from_crash_symptom"] is True
    assert s["cause_crash_link_seen"] is True
    assert s["strict_root_cause_understood"] is True


def test_crash_line_called_root_cause_is_caught(tmp_path: Path) -> None:
    """Agent points root cause at the crash line (214) instead of the fault (201)."""
    gt = _base_gt()
    events = [
        _rec(0, "root_cause", file="tga.c", function="decode_rle", line=214, var="out"),
        _rec(1, "sink", file="tga.c", function="decode_rle", line=214, var="out"),
    ]
    s = T3RootCauseEvaluator().evaluate(_bundle(tmp_path, gt, events))["summary"]
    assert s["root_cause_status"] != "located"
    assert s["mistook_crash_for_cause"] is True
    assert s["strict_root_cause_understood"] is False


def test_located_but_not_linked_fails_strict_passes_lenient(tmp_path: Path) -> None:
    gt = _base_gt()
    events = [
        _rec(0, "root_cause", file="tga.c", function="decode_rle", line=201, var="count"),
        _rec(1, "sink", file="tga.c", function="decode_rle", line=214, var="out"),
        # an edge that touches the cause var but never reaches the sink var
        _rec(2, "edge", file="tga.c", function="decode_rle", line=201, type="data",
             relation="propagates", **{"from": "len", "to": "count"}),
    ]
    s = T3RootCauseEvaluator().evaluate(_bundle(tmp_path, gt, events))["summary"]
    assert s["distinguishes_cause_from_crash_symptom"] is True
    assert s["cause_crash_link_seen"] is False
    assert s["strict_root_cause_understood"] is False
    assert s["lenient_root_cause_understood"] is True  # cause var participates in an edge


def test_no_vars_degrades_to_position_only(tmp_path: Path) -> None:
    """Legacy GT without vars: strict falls back to the position verdict."""
    gt = _base_gt()
    gt["root_cause"].pop("var"); gt["sink"].pop("var")
    events = [
        _rec(0, "root_cause", file="tga.c", function="decode_rle", line=201),
        _rec(1, "sink", file="tga.c", function="decode_rle", line=214),
    ]
    s = T3RootCauseEvaluator().evaluate(_bundle(tmp_path, gt, events))["summary"]
    assert s["cause_crash_link_evaluable"] is False
    assert s["distinguishes_cause_from_crash_symptom"] is True
    assert s["strict_root_cause_understood"] is True  # position-only fallback


def test_non_distinguishable_gt_not_penalized(tmp_path: Path) -> None:
    """When the fault line IS the crash line, there is no distinction to require."""
    gt = _base_gt()
    gt["root_cause"] = {"file": "tga.c", "function": "decode_rle", "line": 214, "var": "out"}
    events = [
        _rec(0, "root_cause", file="tga.c", function="decode_rle", line=214, var="out"),
        _rec(1, "sink", file="tga.c", function="decode_rle", line=214, var="out"),
        _rec(2, "edge", file="tga.c", function="decode_rle", line=214, type="control",
             relation="missing_check", **{"from": "out", "to": "out"}),
    ]
    s = T3RootCauseEvaluator().evaluate(_bundle(tmp_path, gt, events))["summary"]
    assert s["gt_distinguishable"] is False
    assert s["strict_root_cause_understood"] is True
