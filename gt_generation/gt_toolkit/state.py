"""Canonical sample_state.json management.

This reconciles the two historical state shapes (the orchestrator doc vs the old
init_sample_state.py) into one superset. All stages/roles read and write the
same file via this module, so there is a single state contract regardless of
which coding-agent CLI drives the run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

STAGES = [
    "materialize",
    "reproduce",
    "generate_gt",
    "source_audit",
    "semantic_review",
    "runtime_validate",
    "cleanup",
]


def default_state(sample_id: str) -> dict[str, Any]:
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
            "primary_detector": "",
            "primary_detector_crash_observed": False,
            "secondary_detector": "",
            "secondary_detector_crash_observed": False,
            "cross_tool_confirmed": False,
            "patch_resolves": False,
            "reproduction_rate": 0.0,
            "flaky": False,
            "matches_issue_description": False,
        },
        "coverage": {
            "checked": False,
            "covered_gt_locations": 0,
            "missing_gt_locations": 0,
            "watchpoint_checked": False,
            "watchpoint_precision_valid": False,
        },
        "validation": {
            "schema_valid": False,
            "sanitizer_grounding_valid": False,
            "patch_sanity_valid": False,
            "trace_semantics_valid": False,
            "source_sink_valid": False,
            "root_cause_matches_patch": False,
            "root_cause_grounding_strength": "",
            "multi_location_patch": False,
            "requires_human_review": True,
        },
        "cleanup": {"source_deleted": False, "build_deleted": False},
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """Fill missing keys from base without clobbering existing override values."""
    for key, value in base.items():
        if key not in override:
            override[key] = value
        elif isinstance(value, dict) and isinstance(override.get(key), dict):
            _deep_merge(value, override[key])
    return override


def load_or_init(path: Path, sample_id: str) -> dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        return _deep_merge(default_state(sample_id), state)
    return default_state(sample_id)


def _coerce(value: str, as_json: bool) -> Any:
    if not as_json:
        return value
    return json.loads(value)


def _set_nested(state: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = state
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gt-toolkit state", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create or upgrade a sample_state.json.")
    p_init.add_argument("--sample-id", required=True)
    p_init.add_argument("--output", required=True, type=Path)

    p_set = sub.add_parser("set", help="Set one dotted key, e.g. reproduction.flaky.")
    p_set.add_argument("--file", required=True, type=Path)
    p_set.add_argument("--sample-id", default="")
    p_set.add_argument("--key", required=True)
    p_set.add_argument("--value", required=True)
    p_set.add_argument("--json", action="store_true", help="Parse --value as JSON.")

    p_stage = sub.add_parser("complete-stage", help="Mark a stage completed and advance status.")
    p_stage.add_argument("--file", required=True, type=Path)
    p_stage.add_argument("--sample-id", default="")
    p_stage.add_argument("--stage", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "init":
        state = load_or_init(args.output, args.sample_id)
        _write(args.output, state)
        print(json.dumps({"ok": True, "output": str(args.output)}))
        return 0

    if args.cmd == "set":
        sample_id = args.sample_id or args.file.parent.name
        state = load_or_init(args.file, sample_id)
        _set_nested(state, args.key, _coerce(args.value, args.json))
        _write(args.file, state)
        print(json.dumps({"ok": True, "key": args.key}))
        return 0

    if args.cmd == "complete-stage":
        sample_id = args.sample_id or args.file.parent.name
        state = load_or_init(args.file, sample_id)
        completed = state.setdefault("completed_stages", [])
        if args.stage not in completed:
            completed.append(args.stage)
        state["current_stage"] = args.stage
        if args.stage == STAGES[-1]:
            state["status"] = "completed"
        elif state.get("status") in ("not_started", ""):
            state["status"] = "running"
        _write(args.file, state)
        print(json.dumps({"ok": True, "completed_stages": completed}))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
