"""OpenHands trajectory parsing utilities.

This parser extracts deterministic evidence from the saved OpenHands
trajectory. It does not decide whether the agent is correct; metric classes
perform matching against ground truth.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


SED_RANGE_RE = re.compile(
    r"sed\s+-n\s+['\"]?(?P<start>\d+)\s*,\s*(?P<end>\d+)p['\"]?\s+(?P<path>[^\s;&|]+)"
)
NL_SED_RANGE_RE = re.compile(
    r"(?:nl\s+-ba|cat\s+-n)\s+(?P<path>[^\s;&|]+).*?"
    r"sed\s+-n\s+['\"]?(?P<start>\d+)\s*,\s*(?P<end>\d+)p['\"]?",
    re.DOTALL,
)
GREP_FILE_RE = re.compile(r"\b(?:grep|rg)\b[^\n;&|]*\s+(?P<path>[^\s;&|]+\.(?:c|cc|cpp|cxx|h|hpp|hh|py|rs|go|java))")


@dataclass(frozen=True)
class ViewedRange:
    path: str
    start: int
    end: int
    event_index: int
    command: str


@dataclass(frozen=True)
class EvidenceEvent:
    index: int
    source: str | None
    action: str | None
    text: str
    command: str | None = None
    thought: str | None = None
    message: str | None = None
    phase: str = "pre_submit"


@dataclass
class TrajectoryEvidence:
    path: Path
    events: list[EvidenceEvent] = field(default_factory=list)
    viewed_ranges: list[ViewedRange] = field(default_factory=list)
    submit_event_index: int | None = None

    def events_for_phase(self, phase: str) -> list[EvidenceEvent]:
        if phase == "all":
            return self.events
        if phase == "pre_submit":
            return [event for event in self.events if event.phase == "pre_submit"]
        if phase == "post_submit":
            return [event for event in self.events if event.phase == "post_submit"]
        raise ValueError(f"Unknown phase: {phase}")

    def viewed_ranges_for_phase(self, phase: str) -> list[ViewedRange]:
        if phase == "all":
            return self.viewed_ranges
        if self.submit_event_index is None:
            return self.viewed_ranges if phase == "pre_submit" else []
        if phase == "pre_submit":
            return [item for item in self.viewed_ranges if item.event_index < self.submit_event_index]
        if phase == "post_submit":
            return [item for item in self.viewed_ranges if item.event_index >= self.submit_event_index]
        raise ValueError(f"Unknown phase: {phase}")


def load_openhands_trajectory(path: Path) -> TrajectoryEvidence:
    raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected OpenHands trajectory list, got {type(raw).__name__}")

    submit_index = _find_submit_index(raw)
    evidence = TrajectoryEvidence(path=path, submit_event_index=submit_index)

    for idx, item in enumerate(raw):
        strings = list(_iter_strings(item))
        text = "\n".join(strings)
        args = item.get("args") if isinstance(item, dict) else {}
        command = args.get("command") if isinstance(args, dict) else None
        thought = args.get("thought") if isinstance(args, dict) else None
        message = item.get("message") if isinstance(item, dict) else None
        phase = "post_submit" if submit_index is not None and idx >= submit_index else "pre_submit"
        event = EvidenceEvent(
            index=idx,
            source=item.get("source") if isinstance(item, dict) else None,
            action=item.get("action") if isinstance(item, dict) else None,
            text=text,
            command=command,
            thought=thought,
            message=message,
            phase=phase,
        )
        evidence.events.append(event)
        if command:
            evidence.viewed_ranges.extend(_extract_viewed_ranges(command, idx))

    return evidence


def _find_submit_index(raw: list[Any]) -> int | None:
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        if item.get("source") != "agent" or item.get("action") != "run":
            continue
        args = item.get("args")
        command = args.get("command") if isinstance(args, dict) else None
        if command and "submit.sh" in command:
            return idx
    return None


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _extract_viewed_ranges(command: str, event_index: int) -> list[ViewedRange]:
    ranges: list[ViewedRange] = []
    for match in NL_SED_RANGE_RE.finditer(command):
        ranges.append(
            ViewedRange(
                path=_normalize_path(match.group("path")),
                start=int(match.group("start")),
                end=int(match.group("end")),
                event_index=event_index,
                command=command,
            )
        )
    for match in SED_RANGE_RE.finditer(command):
        ranges.append(
            ViewedRange(
                path=_normalize_path(match.group("path")),
                start=int(match.group("start")),
                end=int(match.group("end")),
                event_index=event_index,
                command=command,
            )
        )
    return ranges


def _normalize_path(path: str) -> str:
    path = path.strip("'\"")
    marker = "/src-vul/"
    if marker in path:
        return path.split(marker, 1)[1]
    marker = "/src/"
    if marker in path:
        return path.split(marker, 1)[1]
    return path.lstrip("./")


def path_suffix_matches(gt_path: str, observed_path: str) -> bool:
    gt = _normalize_path(gt_path)
    obs = _normalize_path(observed_path)
    return obs.endswith(gt) or gt.endswith(obs)
