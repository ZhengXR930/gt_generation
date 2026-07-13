"""Read the agent's structured reasoning-recorder events bundled with a GT."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def trajectory_path_for_gt(gt_path: Path) -> Path:
    return gt_path.parent.parent / "openhands_log" / "trajectory"


def load_recorder_events(gt_path: Path) -> list[dict[str, Any]]:
    """The non-retracted `record_reasoning` records saved next to the GT."""
    events = load_native_recorder_events(gt_path)
    retracted = {item.get("retracts") for item in events if item.get("kind") == "retraction"}
    return [item for item in events if item.get("id") not in retracted]


def load_native_recorder_events(gt_path: Path) -> list[dict[str, Any]]:
    path = trajectory_path_for_gt(gt_path)
    if not path.exists():
        return []
    try:
        trajectory = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    if not isinstance(trajectory, list):
        return []
    events: list[dict[str, Any]] = []
    for index, item in enumerate(trajectory):
        if not isinstance(item, dict) or item.get("action") != "record_reasoning":
            continue
        args = dict(item.get("args") or {})
        args["id"] = item.get("id", index)
        args["event_id"] = item.get("id", index)
        args["trajectory_index"] = index
        if "from" not in args and "from_" in args:
            args["from"] = args.get("from_")
        if args.get("kind") == "retraction" or args.get("status") == "retracted":
            continue
        events.append(args)
    return events
