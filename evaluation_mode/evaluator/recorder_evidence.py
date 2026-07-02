"""Structured reasoning-recorder evidence for evaluators."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .trajectory import EvidenceEvent, TrajectoryEvidence


def recorder_path_for_gt(gt_path: Path) -> Path:
    """Return the native OpenHands trajectory path for recorder evidence."""
    return trajectory_path_for_gt(gt_path)


def load_recorder_events(gt_path: Path) -> list[dict[str, Any]]:
    events = load_native_recorder_events(gt_path)
    retracted = {item.get("retracts") for item in events if item.get("kind") == "retraction"}
    return [item for item in events if item.get("id") not in retracted]


def trajectory_path_for_gt(gt_path: Path) -> Path:
    return gt_path.parent.parent / "openhands_log" / "trajectory"


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


def recorder_as_trajectory(gt_path: Path) -> TrajectoryEvidence | None:
    events = load_recorder_events(gt_path)
    if not events:
        return None
    path = recorder_path_for_gt(gt_path)
    evidence = TrajectoryEvidence(path=path, submit_event_index=None)
    for idx, item in enumerate(item for item in events if _is_evaluable_event(item)):
        text = _event_text(item)
        evidence.events.append(
            EvidenceEvent(
                index=idx,
                source="recorder",
                action=str(item.get("kind") or ""),
                text=text,
                command=None,
                thought=text,
                message=None,
                phase="pre_submit",
            )
        )
    return evidence if evidence.events else None


def _is_evaluable_event(item: dict[str, Any]) -> bool:
    if item.get("status") != "confirmed":
        return False
    if not item.get("file") or not item.get("function") or item.get("line") is None:
        return False
    if item.get("kind") == "source" and item.get("function") == "LLVMFuzzerTestOneInput":
        return False
    if item.get("kind") == "edge":
        return bool(
            item.get("role")
            and item.get("from")
            and item.get("to")
            and item.get("relation")
            and item.get("code")
        )
    if item.get("kind") in {"source", "sink", "root_cause"}:
        return bool(item.get("var") and item.get("code"))
    return False


def _event_text(item: dict[str, Any]) -> str:
    fields = [
        ("kind", item.get("kind")),
        ("status", item.get("status")),
        ("file", item.get("file")),
        ("function", item.get("function")),
        ("line", item.get("line")),
        ("var", item.get("var")),
        ("role", item.get("role")),
        ("code", item.get("code")),
        ("from", item.get("from")),
        ("to", item.get("to")),
        ("relation", item.get("relation")),
        ("covered_roles", item.get("covered_roles")),
        ("missing_roles", item.get("missing_roles")),
        ("text", item.get("text")),
        ("evidence", item.get("evidence")),
    ]
    return "\n".join(f"{key}: {value}" for key, value in fields if value not in (None, ""))
