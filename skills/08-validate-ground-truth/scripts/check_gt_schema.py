#!/usr/bin/env python3
"""Lightweight schema check for fine-grained memory-safety GT JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_KEYS = {
    "sample_id",
    "vuln_id",
    "project",
    "classification",
    "source",
    "sink",
    "root_cause",
    "coarse_trace",
    "fine_trace",
    "root_cause_analysis",
    "poc",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ground_truth")
    args = parser.parse_args()
    data = json.loads(Path(args.ground_truth).read_text())
    errors = []

    legacy = {"binary_gt", "trace_hint", "slice_gt", "execution_trace", "trace", "call_chain", "data_flow_chain", "patch"}
    present_legacy = sorted(legacy & set(data))
    if present_legacy:
        errors.append(f"legacy or expanded keys are not allowed: {', '.join(present_legacy)}")

    missing = sorted(REQUIRED_KEYS - set(data))
    if missing:
        errors.append(f"missing keys: {', '.join(missing)}")

    project = data.get("project", {})
    if not isinstance(project, dict):
        errors.append("project must be an object")
    else:
        for key in ["id", "repo", "vulnerable_commit", "fixed_commit"]:
            if key not in project:
                errors.append(f"project missing {key}")

    classification = data.get("classification", {})
    if not isinstance(classification, dict):
        errors.append("classification must be an object")
    else:
        for key in ["class", "cwe"]:
            if key not in classification:
                errors.append(f"classification missing {key}")

    for loc_key in ["source", "sink", "root_cause"]:
        loc = data.get(loc_key, {})
        if not isinstance(loc, dict):
            errors.append(f"{loc_key} must be an object")
            continue
        for key in ["file", "function", "line", "description"]:
            if key not in loc:
                errors.append(f"{loc_key} missing {key}")

    coarse_trace = data.get("coarse_trace")
    if not isinstance(coarse_trace, list) or not coarse_trace:
        errors.append("coarse_trace must be a non-empty list")
    else:
        for idx, step in enumerate(coarse_trace):
            if not isinstance(step, dict):
                errors.append(f"coarse_trace[{idx}] must be an object")
                continue
            for key in ["step", "file", "function", "role", "summary"]:
                if key not in step:
                    errors.append(f"coarse_trace[{idx}] missing {key}")

    fine_trace = data.get("fine_trace")
    if not isinstance(fine_trace, list) or not fine_trace:
        errors.append("fine_trace must be a non-empty list")
    else:
        for idx, step in enumerate(fine_trace):
            if not isinstance(step, dict):
                errors.append(f"fine_trace[{idx}] must be an object")
                continue
            for key in ["step", "file", "function", "line", "role", "var", "code", "note"]:
                if key not in step:
                    errors.append(f"fine_trace[{idx}] missing {key}")

    root_cause_analysis = data.get("root_cause_analysis")
    if not isinstance(root_cause_analysis, dict):
        errors.append("root_cause_analysis must be an object")
    else:
        for key in ["summary", "key_mechanism", "why_patch_works"]:
            if key not in root_cause_analysis:
                errors.append(f"root_cause_analysis missing {key}")

    if not isinstance(data.get("poc"), dict) or not data.get("poc"):
        errors.append("poc must be a non-empty object")
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
