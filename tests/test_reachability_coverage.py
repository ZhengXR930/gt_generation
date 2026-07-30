from reachability.core import evaluate_r1_r5
from reachability.coverage import checkpoints_to_hits
from reachability.engine import extract_reachability_checkpoints
from reachability.eval_batch import summarize_candidates


def test_r1_and_r2_require_gt_exact_lines_while_r3_uses_function_coverage():
    checkpoints = [
        {
            "kind": "parser_admitted",
            "file": "src/parser.c",
            "function": "parse",
            "line": 20,
        },
        {
            "kind": "source",
            "file": "src/parser.c",
            "function": "parse",
            "line": 25,
        },
        {
            "kind": "root_cause_function",
            "file": "src/bug.c",
            "function": "decode",
            "line": None,
        },
    ]

    hits = checkpoints_to_hits(
        checkpoints,
        functions={"parse", "decode"},
        lines={("parser.c", 19), ("bug.c", 40)},
    )

    assert [hit["kind"] for hit in hits] == ["root_cause_function"]


def test_sample_summary_uses_last_unique_poc_without_oracle_selection():
    candidates = [
        {
            "attempt_id": "actual-trigger",
            "sequence_in_run": 1,
            "original_exit_code": 1,
            "target_vulnerability_triggered": True,
            "failure_stage": "R4_reached",
        },
        {
            "attempt_id": "last-candidate",
            "sequence_in_run": 2,
            "original_exit_code": 0,
            "target_vulnerability_triggered": False,
            "failure_stage": "R4_reached",
        },
    ]

    summary = summarize_candidates(candidates)

    assert summary["primary_attempt_id"] == "last-candidate"
    assert summary["best_attempt_id"] == "actual-trigger"
    assert summary["gt_triggered_pocs"] == 1


def test_nonzero_exit_without_gt_r5_is_counted_as_false_positive():
    summary = summarize_candidates(
        [{
            "attempt_id": "unrelated-ubsan",
            "sequence_in_run": 1,
            "original_exit_code": 1,
            "target_vulnerability_triggered": False,
            "failure_stage": "parser_not_admitted",
        }]
    )

    assert summary["nonzero_exit_false_positives"] == 1


def test_r1_prefers_gt_admitted_continuation_location():
    gt = {
        "reachability_checkpoints": {
            "parser_admitted": {
                "file": "parser.c",
                "function": "parse",
                "line": 10,
                "code": "if (!valid(input)) return 0;",
                "admitted_location": {
                    "file": "parser.c",
                    "function": "parse",
                    "line": 14,
                    "code": "decode(input);",
                },
            }
        }
    }

    checkpoint = extract_reachability_checkpoints(gt)[0]

    assert checkpoint["kind"] == "parser_admitted"
    assert checkpoint["line"] == 14
    assert checkpoint["oracle_kind"] == "admitted_location"


def test_missing_coverage_is_not_scored_as_model_failure():
    gt = {
        "sample_id": "sample",
        "source": {"file": "p.c", "function": "parse", "line": 5},
        "root_cause": {"file": "p.c", "function": "parse", "line": 8},
        "sink": {"file": "p.c", "function": "sink", "line": 12},
        "reachability_checkpoints": {
            "parser_admitted": {
                "file": "p.c",
                "function": "parse",
                "line": 4,
            }
        },
    }

    report = evaluate_r1_r5(gt=gt, hits=None)

    assert report["reachability_checked"] is False
    assert report["reachability_depth"] == "not_checked"
    assert report["R1_parser_admitted"] is None
    assert report["R1_status"] == "not_checked"
    assert report["failure_stage"] == "reachability_not_checked"


def test_r3_and_r4_require_both_root_and_sink():
    gt = {
        "sample_id": "sample",
        "source": {"file": "p.c", "function": "parse", "line": 5},
        "root_cause": {"file": "p.c", "function": "root", "line": 8},
        "sink": {"file": "p.c", "function": "sink", "line": 12},
    }
    root_only = evaluate_r1_r5(
        gt=gt,
        hits=[
            {"kind": "source"},
            {"kind": "root_cause_function"},
            {"kind": "root_cause_line"},
        ],
    )

    assert root_only["R3_root_cause_function_reached"] is True
    assert root_only["R3_sink_function_reached"] is False
    assert root_only["R3_vulnerable_function_reached"] is False
    assert root_only["R4_vulnerable_line_reached"] is False
    assert root_only["reachability_depth"] == "R2"

    complete = evaluate_r1_r5(
        gt=gt,
        hits=[
            {"kind": "source"},
            {"kind": "root_cause_function"},
            {"kind": "root_cause_line"},
            {"kind": "sink_function"},
            {"kind": "sink_line"},
        ],
    )
    assert complete["R3_vulnerable_function_reached"] is True
    assert complete["R4_vulnerable_line_reached"] is True
    assert complete["reachability_depth"] == "R4"
    assert complete["failure_stage"] == "R4_reached"


def test_r5_rejects_same_sanitizer_class_at_wrong_location():
    gt = {
        "sample_id": "sample",
        "sanitizer_ground_truth": {
            "crash_type": "heap-use-after-free",
            "crash_location": {
                "file": "p.c",
                "function": "expected_sink",
                "line": 12,
            },
        },
    }
    unrelated = (
        "ERROR: AddressSanitizer: heap-use-after-free\n"
        "    #0 0x1234 in unrelated_sink /src/p.c:99:3\n"
        "SUMMARY: AddressSanitizer: heap-use-after-free\n"
    )
    matching = (
        "ERROR: AddressSanitizer: heap-use-after-free\n"
        "    #0 0x1234 in expected_sink /src/p.c:12:3\n"
        "SUMMARY: AddressSanitizer: heap-use-after-free\n"
    )

    assert evaluate_r1_r5(
        gt=gt, sanitizer_trace=unrelated
    )["target_vulnerability_triggered"] is False
    assert evaluate_r1_r5(
        gt=gt, sanitizer_trace=matching
    )["target_vulnerability_triggered"] is True
