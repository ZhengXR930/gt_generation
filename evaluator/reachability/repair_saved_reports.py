#!/usr/bin/env python3
"""Rebuild GT reachability reports from already-saved runtime artifacts.

No target is built or executed.  Existing reports retain their frozen
checkpoints and hit ledger; a missing report is reconstructed from the frozen
assertion trace, assertion spec, invariant locations, and sanitizer trace.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evaluator.reachability.core import evaluate_r1_r5
from evaluator.reachability.engine import (
    extract_assertion_event_checkpoints,
    extract_reachability_checkpoints,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _assertion_spec(gt_dir: Path) -> dict[str, Any]:
    # Candidate assertions are used only to recover event roles from a saved
    # assertion trace.  Scoring truth remains in assertion_results.json.
    candidate = gt_dir / "candidate_assertions.json"
    return _load(candidate if candidate.is_file() else gt_dir / "verified_assertions.json")


def rebuild_report(gt_dir: Path) -> dict[str, Any]:
    gt = _load(gt_dir / "ground_truth.json")
    sanitizer_trace = (gt_dir / "sanitizer_trace.txt").read_text(
        encoding="utf-8", errors="replace"
    )
    report_path = gt_dir / "reachability_report.json"
    if report_path.is_file():
        previous = _load(report_path)
        checkpoints = [
            item for item in previous.get("checkpoints", [])
            if isinstance(item, dict)
        ]
        hits = [
            item for item in previous.get("hit_locations", [])
            if isinstance(item, dict)
        ]
        evidence_mode = "existing_checkpoint_and_hit_ledger"
    else:
        trace_text = (gt_dir / "vulnerable_assertion_trace.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        assertion_checkpoints = extract_assertion_event_checkpoints(
            codebase=gt_dir / "_work" / "src",
            trace_text=trace_text,
            assertion_spec=_assertion_spec(gt_dir),
            verified_invariants=_load(gt_dir / "verified_invariants.json"),
            sink_location=gt.get("sink") or {},
        )
        checkpoints = extract_reachability_checkpoints(gt) + assertion_checkpoints
        hits = [
            {
                "kind": "assertion_event",
                "event_point": item["event_point"],
                "assertion_role": item.get("assertion_role", []),
            }
            for item in assertion_checkpoints
        ]
        evidence_mode = "frozen_assertion_and_sanitizer_traces"

    report = evaluate_r1_r5(
        gt=gt,
        hits=hits,
        sanitizer_trace=sanitizer_trace,
        checkpoints=checkpoints,
    )
    report["reconstruction"] = {
        "executed_target": False,
        "evidence_mode": evidence_mode,
        "sanitizer_trace": "sanitizer_trace.txt",
        "assertion_trace": (
            "vulnerable_assertion_trace.txt"
            if (gt_dir / "vulnerable_assertion_trace.txt").is_file()
            else None
        ),
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sample_ids", nargs="+")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for sample_id in args.sample_ids:
        gt_dir = GT_RESULTS / sample_id
        report = rebuild_report(gt_dir)
        summary = {
            "sample_id": sample_id,
            "reachability_depth": report.get("reachability_depth"),
            "R1": report.get("R1_parser_admitted"),
            "R2": report.get("R2_source_reached"),
            "R3_root": report.get("R3_root_cause_function_reached"),
            "R3_sink": report.get("R3_sink_function_reached"),
            "R4_root": report.get("R4_root_cause_line_reached"),
            "R4_sink": report.get("R4_sink_line_reached"),
            "target": report.get("target_vulnerability_triggered"),
        }
        print(json.dumps(summary, ensure_ascii=False))
        if args.apply:
            (gt_dir / "reachability_report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )


if __name__ == "__main__":
    main()
