#!/usr/bin/env python3
"""Lightweight schema check for Veritas-style memory-safety GT JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_VULN_KEYS = {
    "vuln_id",
    "class",
    "cwe",
    "before_commit",
    "after_commit",
    "project_id",
    "sink",
    "source",
    "trace_hint",
    "call_chain",
    "binary_gt",
    "slice_gt",
    "data_flow_chain",
    "root_cause",
    "poc",
    "patch",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ground_truth")
    args = parser.parse_args()
    data = json.loads(Path(args.ground_truth).read_text())
    errors = []
    vulns = data.get("ground_truth", {}).get("vulnerabilities")
    if not isinstance(vulns, list) or not vulns:
        errors.append("ground_truth.vulnerabilities must be a non-empty list")
    else:
        for idx, vuln in enumerate(vulns):
            missing = sorted(REQUIRED_VULN_KEYS - set(vuln))
            if missing:
                errors.append(f"vulnerabilities[{idx}] missing keys: {', '.join(missing)}")
            for loc_key in ["source", "sink"]:
                loc = vuln.get(loc_key, {})
                if not isinstance(loc, dict):
                    errors.append(f"vulnerabilities[{idx}].{loc_key} must be an object")
                    continue
                for key in ["file", "function", "line", "note"]:
                    if key not in loc:
                        errors.append(f"vulnerabilities[{idx}].{loc_key} missing {key}")
            if not vuln.get("data_flow_chain"):
                errors.append(f"vulnerabilities[{idx}].data_flow_chain is empty")
            if not vuln.get("root_cause"):
                errors.append(f"vulnerabilities[{idx}].root_cause is empty")
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
