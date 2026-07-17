import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_ENV = {**os.environ, "PYTHONPATH": str(ROOT / "gt_generation") + os.pathsep + os.environ.get("PYTHONPATH", "")}


def _base_gt() -> dict:
    loc = {
        "file": "parser.c",
        "function": "parse",
        "line": 10,
        "value_from": "PoC file bytes parsed by parse().",
        "description": "test location",
    }
    return {
        "sample_id": "sample",
        "vuln_id": "vuln",
        "project": {
            "id": "proj",
            "repo": "https://example.invalid/repo",
            "vulnerable_commit": "old",
            "fixed_commit": "new",
        },
        "classification": {"class": "heap-buffer-overflow", "cwe": "CWE-122"},
        "bug_description": {
            "original": "original report",
            "original_source": "test",
            "normalized": "normalized report",
        },
        "source": {**loc, "trace_step": 1},
        "sink": {**loc, "trace_step": 2},
        "reachability_checkpoints": {
            "parser_admitted": {
                "file": "parser.c",
                "function": "parse",
                "line": 10,
                "description": "PoC reaches parse().",
            }
        },
        "tainted_value_origin": {
            **loc,
            "var": "len",
            "code": "len = input[0]",
        },
        "coarse_trace": [
            {
                "step": 1,
                "file": "parser.c",
                "function": "parse",
                "summary": "summary",
            }
        ],
        "fine_trace": [
            {
                "step": 1,
                "file": "parser.c",
                "function": "parse",
                "line": 10,
                "var": "len",
                "code": "len = input[0]",
                "note": "note",
            },
            {
                "step": 2,
                "file": "parser.c",
                "function": "parse",
                "line": 10,
                "var": "len",
                "code": "buf[len]",
                "note": "note",
            },
        ],
        "root_cause": {
            **loc,
            "trace_step": 2,
            "description": "Missing bounds check before sink.",
        },
        "sanitizer_ground_truth": {
            "detector": "asan",
            "trace_format": "asan",
            "sanitizer": "AddressSanitizer",
            "crash_type": "heap-buffer-overflow",
            "access_type": "READ",
            "access_size": 1,
            "crash_location": {"file": "parser.c", "function": "parse", "line": 10},
            "allocation_context": None,
            "free_context": None,
            "crash_stack": [{"frame": 0, "function": "parse", "file": "parser.c", "line": 10}],
            "patch_resolves": True,
            "cross_tool_confirmed": False,
            "reproduction_rate": 1.0,
            "flaky": False,
            "cross_validation": {
                "sink_matches_crash": True,
                "trace_consistent_with_stack": True,
                "tainted_value_reaches_sink": True,
            },
        },
        "poc": {
            "path": "poc",
            "trigger": "./target {poc}",
            "format": {
                "name": "toy-container",
                "contract": "Header must parse and carry a length field that reaches the sink.",
            },
        },
    }


def _run_validate(gt: dict, tmp_path: Path) -> dict:
    path = tmp_path / "ground_truth.json"
    path.write_text(json.dumps(gt), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "gt_toolkit", "validate", str(path)],
        cwd=ROOT,
        env=_ENV,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out = json.loads(proc.stdout)
    out["returncode"] = proc.returncode
    return out


def test_poc_format_is_required(tmp_path: Path) -> None:
    gt = _base_gt()
    del gt["poc"]["format"]

    result = _run_validate(gt, tmp_path)

    assert result["ok"] is False
    assert result["returncode"] == 1
    assert "poc missing format" in result["errors"]


def test_poc_format_accepts_complete_contract(tmp_path: Path) -> None:
    result = _run_validate(_base_gt(), tmp_path)

    # A complete GT has no errors (warnings are non-fatal by design).
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert result["errors"] == []


def test_poc_format_contract_is_required(tmp_path: Path) -> None:
    gt = _base_gt()
    del gt["poc"]["format"]["contract"]

    result = _run_validate(gt, tmp_path)

    assert result["ok"] is False
    assert "poc.format missing contract" in result["errors"]


def test_trace_semantic_labels_are_rejected(tmp_path: Path) -> None:
    gt = _base_gt()
    gt["coarse_trace"][0]["role"] = "source_and_parse"
    gt["fine_trace"][0]["kind"] = "input_materialization"

    result = _run_validate(gt, tmp_path)

    assert result["ok"] is False
    assert "coarse_trace[0] must not contain role or kind" in result["errors"]
    assert "fine_trace[0] must not contain role or kind" in result["errors"]
