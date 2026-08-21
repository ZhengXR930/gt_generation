from reachability.core import evaluate_r1_r5
from reachability.engine import (
    extract_assertion_event_checkpoints,
    extract_reachability_checkpoints,
)
from reachability.cli import main as reachability_main


def test_assertion_trace_resolves_event_breakpoints_from_gt_codebase(tmp_path):
    source = tmp_path / "src" / "parser.c"
    source.parent.mkdir()
    source.write_text(
        "static int parse_count(int raw) {\n"
        "  fprintf(stderr, \"ASSERT_EVT point=range_decision raw=%d\\n\", raw);\n"
        "  fprintf(stderr, \"ASSERT_EVT point=protected_read size=%d\\n\", raw);\n"
        "  return raw;\n"
        "}\n"
    )
    trace = (
        "CASE name=original rc=1 result=crash\n"
        "ASSERT_EVT point=range_decision raw=-9\n"
        "ASSERT_EVT point=protected_read size=72\n"
        "ENDCASE\n"
    )
    spec = {
        "original_case": "original",
        "assertions": [{
            "id": "required.bound",
            "kind": "required",
            "at": "range_decision",
            "protects": "protected_read",
        }],
    }

    checkpoints = extract_assertion_event_checkpoints(
        codebase=tmp_path,
        trace_text=trace,
        assertion_spec=spec,
    )

    assert [(item["event_point"], item["line"]) for item in checkpoints] == [
        ("range_decision", 2),
        ("protected_read", 3),
    ]
    assert checkpoints[0]["function"] == "parse_count"
    assert checkpoints[0]["assertion_role"] == ["root"]
    assert checkpoints[1]["assertion_role"] == ["protected", "sink"]


def test_assertion_event_hits_report_order_without_promoting_stages(tmp_path):
    source = tmp_path / "bug.c"
    source.write_text(
        "int bug(int n) {\n"
        "  puts(\"ASSERT_EVT point=decision\");\n"
        "  puts(\"ASSERT_EVT point=read\");\n"
        "  return n;\n"
        "}\n"
    )
    trace = (
        "CASE name=original rc=1 result=crash\n"
        "ASSERT_EVT point=decision n=-1\n"
        "ASSERT_EVT point=read n=-1\n"
        "ENDCASE\n"
    )
    spec = {
        "original_case": "original",
        "assertions": [{
            "id": "required.bound",
            "kind": "required",
            "at": "decision",
            "protects": "read",
        }],
    }
    assertion_points = extract_assertion_event_checkpoints(
        codebase=tmp_path,
        trace_text=trace,
        assertion_spec=spec,
    )
    gt = {
        "sample_id": "sample",
        "source": {"file": "bug.c", "function": "bug", "line": 1},
        "root_cause": {"file": "bug.c", "function": "bug", "line": 2},
        "sink": {"file": "bug.c", "function": "bug", "line": 3},
        "reachability_checkpoints": {
            "parser_admitted": {"file": "bug.c", "function": "bug", "line": 1}
        },
    }
    hits = [
        {
            "kind": "assertion_event",
            "event_point": item["event_point"],
            "assertion_role": item["assertion_role"],
        }
        for item in assertion_points
    ]

    report = evaluate_r1_r5(
        gt=gt,
        hits=hits,
        checkpoints=extract_reachability_checkpoints(gt) + assertion_points,
    )

    # The assertion channel reports what the instrumentation observed...
    assert report["assertion_sequence_matches"] is True
    assert report["assertion_event_reachability"] == {"decision": True, "read": True}
    # ...but under location-reachability-v3 it never promotes a location stage:
    # only gdb hits on the GT anchors do, and none were supplied here.
    assert report["R1_parser_admitted"] is False
    assert report["R2_source_reached"] is False
    assert report["R3_root_cause_function_reached"] is False
    assert report["R4_root_cause_line_reached"] is False
    assert report["reachability_depth"] == "R0"


def test_selected_sink_invariant_is_reported_but_does_not_mark_sink_reached(tmp_path):
    source = tmp_path / "src" / "bug.c"
    source.parent.mkdir()
    source.write_text(
        "int sink(int n) {\n"
        '  puts("ASSERT_EVT point=stale_use");\n'
        "  return n;\n"
        "}\n"
    )
    trace = (
        "CASE name=original rc=1 result=crash\n"
        "ASSERT_EVT point=stale_use value=7\n"
        "ENDCASE\n"
    )
    spec = {
        "original_case": "original",
        "assertions": [{
            "id": "propagation",
            "kind": "transition",
            "at": "stale_use",
            "invariants": ["sink.node"],
        }],
    }
    invariants = {
        "nodes": [{
            "invariant_id": "sink.node",
            "file": "bug.c",
            "function": "sink",
            "line": 2,
        }],
        "edges": [],
    }
    sink = {"file": "bug.c", "function": "sink", "line": 2}
    points = extract_assertion_event_checkpoints(
        codebase=tmp_path / "src",
        trace_text=trace,
        assertion_spec=spec,
        verified_invariants=invariants,
        sink_location=sink,
    )
    assert points[0]["assertion_role"] == ["observed", "sink"]

    gt = {
        "sample_id": "sample",
        "source": {"file": "bug.c", "function": "sink", "line": 1},
        "root_cause": {"file": "bug.c", "function": "sink", "line": 1},
        "sink": sink,
    }
    report = evaluate_r1_r5(
        gt=gt,
        hits=[{
            "kind": "assertion_event",
            "event_point": "stale_use",
            "assertion_role": points[0]["assertion_role"],
        }],
        checkpoints=extract_reachability_checkpoints(gt) + points,
    )
    assert report["assertion_event_reachability"] == {"stale_use": True}
    assert report["R3_sink_function_reached"] is False
    assert report["R4_sink_line_reached"] is False


def test_required_protected_event_is_reported_but_does_not_mark_sink_reached(tmp_path):
    source = tmp_path / "src" / "bug.c"
    source.parent.mkdir()
    source.write_text(
        'void bug(void) {\n'
        '  puts("ASSERT_EVT point=decision");\n'
        '  puts("ASSERT_EVT point=unsafe_write");\n'
        '}\n'
    )
    trace = (
        "CASE name=original rc=1 result=crash\n"
        "ASSERT_EVT point=decision count=4 limit=3\n"
        "ASSERT_EVT point=unsafe_write size=4\n"
        "ENDCASE\n"
    )
    spec = {
        "original_case": "original",
        "assertions": [{
            "id": "required.bound",
            "kind": "required",
            "at": "decision",
            "protects": "unsafe_write",
            "invariants": ["root.bound"],
        }],
    }
    points = extract_assertion_event_checkpoints(
        codebase=tmp_path / "src",
        trace_text=trace,
        assertion_spec=spec,
    )
    protected = next(point for point in points if point["event_point"] == "unsafe_write")
    assert protected["assertion_role"] == ["protected", "sink"]

    gt = {
        "sample_id": "sample",
        "source": {"file": "bug.c", "function": "bug", "line": 1},
        "root_cause": {"file": "bug.c", "function": "bug", "line": 2},
        "sink": {"file": "bug.c", "function": "bug", "line": 3},
    }
    report = evaluate_r1_r5(
        gt=gt,
        hits=[{
            "kind": "assertion_event",
            "event_point": "unsafe_write",
            "assertion_role": protected["assertion_role"],
        }],
        checkpoints=extract_reachability_checkpoints(gt) + points,
    )
    # Only the protected event was supplied, so the expected decision ->
    # unsafe_write order is not satisfied and the sink stays unreached.
    assert report["assertion_event_reachability"] == {
        "decision": False, "unsafe_write": True,
    }
    assert report["assertion_sequence_matches"] is False
    assert report["R3_sink_function_reached"] is False
    assert report["R4_sink_line_reached"] is False


def test_cli_uses_frozen_trace_hits_and_writes_package_local_artifacts(
    tmp_path, monkeypatch
):
    source = tmp_path / "src" / "bug.c"
    source.parent.mkdir()
    source.write_text(
        'int bug(int n) {\n'
        '  puts("ASSERT_EVT point=decision");\n'
        '  puts("ASSERT_EVT point=read");\n'
        '  return n;\n'
        '}\n'
    )
    gt = {
        "sample_id": "sample",
        "source": {"file": "bug.c", "function": "bug", "line": 1},
        "root_cause": {"file": "bug.c", "function": "bug", "line": 2},
        "sink": {"file": "bug.c", "function": "sink", "line": 9},
        "reachability_checkpoints": {
            "parser_admitted": {"file": "bug.c", "function": "bug", "line": 1}
        },
        "sanitizer_ground_truth": {"crash_type": "SEGV"},
    }
    spec = {
        "original_case": "original",
        "assertions": [{
            "id": "required.bound",
            "kind": "required",
            "at": "decision",
            "protects": "read",
        }],
    }
    trace = (
        "CASE name=original rc=1 result=crash\n"
        "ASSERT_EVT point=decision n=-1\n"
        "ASSERT_EVT point=read n=-1\n"
        "ENDCASE\n"
    )
    sanitizer = "ERROR: AddressSanitizer: SEGV\n"
    for name, value in [
        ("ground_truth.json", gt),
        ("candidate_assertions.json", spec),
    ]:
        (tmp_path / name).write_text(__import__("json").dumps(value))
    (tmp_path / "vulnerable_assertion_trace.txt").write_text(trace)
    (tmp_path / "sanitizer_trace.txt").write_text(sanitizer)
    out_dir = tmp_path / "reachability"
    monkeypatch.setattr(
        "sys.argv",
        [
            "reachability",
            "--gt", str(tmp_path / "ground_truth.json"),
            "--codebase", str(tmp_path / "src"),
            "--assertion-spec", str(tmp_path / "candidate_assertions.json"),
            "--assertion-trace", str(tmp_path / "vulnerable_assertion_trace.txt"),
            "--sanitizer-trace", str(tmp_path / "sanitizer_trace.txt"),
            "--out-dir", str(out_dir),
        ],
    )

    reachability_main()

    report = __import__("json").loads(
        (out_dir / "reachability_report.json").read_text()
    )
    assert report["reachability_checked"] is True
    assert report["assertion_sequence_matches"] is True
    # The CLI freezes the assertion trace into hits, but those hits are
    # assertion events: under v3 they report the sequence and leave the
    # location stages unreached, because no debugger ran on the GT anchors.
    assert report["R1_parser_admitted"] is False
    assert report["R2_source_reached"] is False
    assert report["R3_root_cause_function_reached"] is False
    assert report["R4_root_cause_line_reached"] is False
    assert report["reachability_depth"] == "R0"
    assert report["artifacts"] == {
        "breakpoints": "reachability/reachability_breakpoints.json",
        "hits": "reachability/reachability_hits.json",
        "sanitizer_trace": "reachability/sanitizer_trace.txt",
        "assertion_spec": "candidate_assertions.json",
        "assertion_trace": "vulnerable_assertion_trace.txt",
    }


def test_cli_falls_back_to_saved_sanitizer_trace(tmp_path, monkeypatch):
    gt = {
        "sample_id": "sample",
        "sanitizer_ground_truth": {
            "detector": "address",
            "crash_type": "heap-buffer-overflow",
            "crash_location": {
                "file": "src/a.c",
                "function": "sink",
                "line": 10,
            },
        },
        "sink": {
            "file": "src/a.c",
            "function": "sink",
            "line": 10,
            "relation": {"op": "eq", "left": "x", "right": "x"},
        },
    }
    saved = (
        "==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1\n"
        "    #0 0x1 in sink src/a.c:10:1\n"
        "SUMMARY: AddressSanitizer: heap-buffer-overflow src/a.c:10:1 in sink\n"
    )
    (tmp_path / "ground_truth.json").write_text(__import__("json").dumps(gt))
    (tmp_path / "sanitizer_trace.txt").write_text(saved)
    out_dir = tmp_path / "reachability"
    monkeypatch.setattr(
        "sys.argv",
        [
            "reachability",
            "--gt", str(tmp_path / "ground_truth.json"),
            "--sanitizer-command", "printf 'language error only'",
            "--sanitizer-trace", str(tmp_path / "sanitizer_trace.txt"),
            "--out-dir", str(out_dir),
        ],
    )

    reachability_main()

    report = __import__("json").loads(
        (out_dir / "reachability_report.json").read_text()
    )
    assert report["target_vulnerability_triggered"] is True
    assert report["sanitizer_observed"]["crash_type"] == "heap-buffer-overflow"
