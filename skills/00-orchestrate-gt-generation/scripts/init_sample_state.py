#!/usr/bin/env python3
"""Create or update a minimal sample_state.json for a GT generation run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def default_state(sample_id: str) -> dict:
    return {
        "sample_id": sample_id,
        "status": "not_started",
        "current_stage": "",
        "completed_stages": [],
        "failure": {"stage": "", "type": "", "message": ""},
        "artifacts": {
            "build_script": "build.sh",
            "sanitizer_trace": "sanitizer_trace.txt",
            "valgrind_trace": "valgrind_trace.txt",
            "ground_truth": "ground_truth.json",
            "generation_log": "generation.log",
        },
        "reproduction": {
            "sanitizer_crash_observed": False,
            "valgrind_crash_observed": False,
            "matches_issue_description": False,
        },
        "coverage": {"checked": False, "covered_gt_locations": 0, "missing_gt_locations": 0},
        "validation": {
            "schema_valid": False,
            "source_sink_valid": False,
            "root_cause_matches_patch": False,
            "requires_human_review": True,
        },
        "cleanup": {"source_deleted": False, "build_deleted": False},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        state = json.loads(path.read_text())
        base = default_state(args.sample_id)
        base.update(state)
        state = base
    else:
        state = default_state(args.sample_id)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
