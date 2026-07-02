"""Export raw context observed by an agent from an OpenHands trajectory.

This module is deliberately recorder-only: it preserves what the agent saw from
read-like tools and shell inspection commands without judging correctness.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


READ_COMMAND_RE = re.compile(
    r"(^|[\s;&|()])"
    r"(?P<tool>cat|find|grep|head|less|ls|nl|rg|sed|stat|tail|tree|wc)"
    r"(\s|$)"
)


def export_raw_context_artifacts(
    trajectory_path: Path, output_dir: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = json.loads(trajectory_path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected OpenHands trajectory list, got {type(raw).__name__}")

    observations = _observations_by_cause(raw)
    submit_index = _find_submit_index(raw)
    records: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or item.get("source") != "agent":
            continue
        action = item.get("action")
        if action == "read":
            records.append(_read_record(index, item, observations, submit_index))
        elif action == "run":
            command = _command(item)
            kinds = _read_command_kinds(command)
            if not kinds:
                continue
            records.append(_run_record(index, item, observations, kinds, submit_index))

    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "raw_context_events.jsonl"
    json_path = output_dir / "raw_context_events.json"
    summary_path = output_dir / "raw_context_summary.json"
    jsonl_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    summary = _summary(records, jsonl_path=jsonl_path, json_path=json_path)
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return records, summary


def _observations_by_cause(raw: list[Any]) -> dict[Any, tuple[int, dict[str, Any]]]:
    observations: dict[Any, tuple[int, dict[str, Any]]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or "observation" not in item:
            continue
        cause = item.get("cause")
        if cause is not None and cause not in observations:
            observations[cause] = (index, item)
    return observations


def _read_record(
    index: int,
    item: dict[str, Any],
    observations: dict[Any, tuple[int, dict[str, Any]]],
    submit_index: int | None,
) -> dict[str, Any]:
    args = item.get("args") if isinstance(item.get("args"), dict) else {}
    obs_index, obs = observations.get(item.get("id"), (None, {}))
    content = _content(obs)
    path = str(args.get("path") or "")
    return _base_record(
        index=index,
        item=item,
        obs_index=obs_index,
        obs=obs,
        submit_index=submit_index,
        kind="openhands_read",
        tool="read",
        query={
            "path": path,
            "start": args.get("start"),
            "end": args.get("end"),
            "view_range": args.get("view_range"),
        },
        content=content,
    )


def _run_record(
    index: int,
    item: dict[str, Any],
    observations: dict[Any, tuple[int, dict[str, Any]]],
    command_kinds: list[str],
    submit_index: int | None,
) -> dict[str, Any]:
    obs_index, obs = observations.get(item.get("id"), (None, {}))
    content = _content(obs)
    return _base_record(
        index=index,
        item=item,
        obs_index=obs_index,
        obs=obs,
        submit_index=submit_index,
        kind="shell_read_command",
        tool="run",
        query={"command": _command(item), "command_kinds": command_kinds},
        content=content,
    )


def _base_record(
    *,
    index: int,
    item: dict[str, Any],
    obs_index: int | None,
    obs: dict[str, Any],
    submit_index: int | None,
    kind: str,
    tool: str,
    query: dict[str, Any],
    content: str,
) -> dict[str, Any]:
    phase = "post_submit" if submit_index is not None and index >= submit_index else "pre_submit"
    args = item.get("args") if isinstance(item.get("args"), dict) else {}
    return {
        "event_index": index,
        "event_id": item.get("id"),
        "timestamp": item.get("timestamp"),
        "phase": phase,
        "kind": kind,
        "tool": tool,
        "thought": args.get("thought"),
        "query": query,
        "observation_index": obs_index,
        "observation_id": obs.get("id"),
        "observation_timestamp": obs.get("timestamp"),
        "observation_type": obs.get("observation"),
        "message": obs.get("message"),
        "content": content,
        "content_chars": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _content(item: dict[str, Any]) -> str:
    value = item.get("content")
    if isinstance(value, str):
        return value
    message = item.get("message")
    return message if isinstance(message, str) else ""


def _command(item: dict[str, Any]) -> str:
    args = item.get("args") if isinstance(item.get("args"), dict) else {}
    return str(args.get("command") or "")


def _read_command_kinds(command: str) -> list[str]:
    seen: list[str] = []
    for match in READ_COMMAND_RE.finditer(command):
        tool = match.group("tool")
        if tool not in seen:
            seen.append(tool)
    return seen


def _find_submit_index(raw: list[Any]) -> int | None:
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or item.get("source") != "agent":
            continue
        if item.get("action") != "run":
            continue
        if "submit.sh" in _command(item):
            return index
    return None


def _summary(
    records: list[dict[str, Any]], *, jsonl_path: Path, json_path: Path
) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    total_chars = 0
    for record in records:
        by_kind[str(record.get("kind"))] = by_kind.get(str(record.get("kind")), 0) + 1
        by_tool[str(record.get("tool"))] = by_tool.get(str(record.get("tool")), 0) + 1
        by_phase[str(record.get("phase"))] = by_phase.get(str(record.get("phase")), 0) + 1
        total_chars += int(record.get("content_chars") or 0)
    return {
        "count": len(records),
        "total_content_chars": total_chars,
        "by_kind": by_kind,
        "by_tool": by_tool,
        "by_phase": by_phase,
        "jsonl_path": str(jsonl_path),
        "json_path": str(json_path),
    }
