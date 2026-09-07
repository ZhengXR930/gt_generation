"""Skill packet copy, lesson addressing, and curator-update application.

Lessons carry stable ids (``- [R.B#3] ...``) so a curator decision can replace
an existing lesson instead of only ever appending one. That is what makes
``MODIFY`` real: merging two lessons is expressed as replacing one of them with
combined text. Each writable section also has a capacity, so once it is full the
only way to say something new is to say it *instead of* something else.

Only the proposal text ever reaches ``SKILL.md``. Evidence stays in the run
artifacts, because ``SKILL.md`` is an input to the agent at test time.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from .defaults import INITIAL_PACKET
from .lint import lint_lesson_text, load_literal_index


class Section(NamedTuple):
    """One writable region of one SKILL.md."""

    path: str
    heading: str
    capacity: int

    @property
    def slug(self) -> str:
        """The id prefix for lessons in this section, e.g. ``R.B``."""
        return self.heading.split()[1]


TARGETS: dict[str, Section] = {
    "reproduction:R.B": Section("reproduction_skill/SKILL.md", "## R.B Learned Reproduction Lessons", 12),
    "submission:S.C": Section("submission_skill/SKILL.md", "## S.C Learned Submission Lessons", 8),
}

# The curator vocabulary is MODIFY / SKIP. ACCEPT and MERGE are tolerated as
# legacy spellings of MODIFY rather than rejected as malformed.
_WRITES = {"MODIFY", "ACCEPT", "MERGE"}

_LESSON_RE = re.compile(r"^-\s+\[([A-Za-z0-9.#_-]+)\]\s*(.*)$")
_BULLET_RE = re.compile(r"^-\s+(.*)$")
_HEADING_RE = re.compile(r"^(#+)\s")


class Lesson(NamedTuple):
    line: int
    lesson_id: str
    text: str


# --------------------------------------------------------------------------- packets


def copy_initial_packet(out: Path, source: Path = INITIAL_PACKET) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(source, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    normalize_packet(out)
    return out


def normalize_packet(packet: Path) -> None:
    """Give every existing lesson bullet a stable id. Idempotent."""
    for section in TARGETS.values():
        path = packet / section.path
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        taken = {lesson.lesson_id for lesson in _read_section(lines, section)}
        next_number = 1
        changed = False
        for index, text in _bullets(lines, _bounds(lines, section.heading)):
            if _LESSON_RE.match(lines[index]):
                continue
            while f"{section.slug}#{next_number}" in taken:
                next_number += 1
            lesson_id = f"{section.slug}#{next_number}"
            taken.add(lesson_id)
            lines[index] = f"- [{lesson_id}] {text}"
            changed = True
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_lessons(packet: Path) -> dict[str, list[dict[str, str]]]:
    """Current lessons per writable target, with ids."""
    return {
        target: [
            {"lesson_id": lesson.lesson_id, "text": lesson.text}
            for lesson in _read_section(_lines(packet / section.path), section)
        ]
        for target, section in TARGETS.items()
    }


def lessons_snapshot(packet: Path) -> dict[str, Any]:
    """Compact view handed to the Teacher and Curator: what exists and how full."""
    lessons = read_lessons(packet)
    return {
        target: {
            "capacity": section.capacity,
            "used": len(lessons[target]),
            "free": max(section.capacity - len(lessons[target]), 0),
            "lessons": lessons[target],
        }
        for target, section in TARGETS.items()
    }


# --------------------------------------------------------------------------- updates


def apply_curator_decisions(
    packet: Path,
    decisions: list[dict[str, Any]],
    *,
    out_packet: Path | None = None,
    literal_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the accepted proposals into a packet and report what happened.

    Each entry in ``applied`` and ``skipped`` carries the ``decision_index`` it
    came from, so a caller can attribute a written lesson back to the decision
    that produced it without having to guess.
    """
    target_packet = out_packet or packet
    if out_packet is not None:
        if out_packet.exists():
            shutil.rmtree(out_packet)
        shutil.copytree(packet, out_packet, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    normalize_packet(target_packet)
    index = literal_index if literal_index is not None else load_literal_index()

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for position, decision in enumerate(decisions):
        outcome = _apply_one(target_packet, decision, index)
        outcome["decision_index"] = position
        outcome["target"] = decision.get("target")
        (applied if "lesson_id" in outcome else skipped).append(outcome)

    _write_packet_changelog(target_packet, applied)
    return {
        "packet": str(target_packet),
        "applied": applied,
        "skipped": skipped,
        "lessons_after": lessons_snapshot(target_packet),
    }




def apply_correction_decision(
    current_packet: Path,
    previous_packet: Path,
    correction: dict[str, Any],
    *,
    out_packet: Path,
    literal_index: dict[str, Any] | None = None,
    allowed_lesson_ids: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Materialize a post-batch correction decision as the next packet."""
    decision = str(correction.get("decision") or "").upper()
    if decision not in {"KEEP", "MODIFY", "REMOVE", "ROLLBACK"}:
        raise ValueError(f"unsupported correction decision {decision!r}")

    source = previous_packet if decision == "ROLLBACK" else current_packet
    if out_packet.exists():
        shutil.rmtree(out_packet)
    shutil.copytree(source, out_packet, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    normalize_packet(out_packet)

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if decision in {"MODIFY", "REMOVE"}:
        operations = correction.get("operations") or []
        if not operations and (correction.get("target") or correction.get("target_lesson_id")):
            operations = [correction]
        for position, operation in enumerate(operations):
            action = str(operation.get("action") or operation.get("decision") or decision).upper()
            target = str(operation.get("target") or "")
            lesson_id = str(operation.get("target_lesson_id") or operation.get("lesson_id") or "")
            if allowed_lesson_ids is not None and (target, lesson_id) not in allowed_lesson_ids:
                item = {"skip_reason": "target_lesson_id was not introduced or modified by the applied updates"}
            elif action == "MODIFY":
                result = apply_curator_decisions(
                    out_packet,
                    [{
                        "decision": "MODIFY",
                        "target": target,
                        "target_lesson_id": lesson_id,
                        "proposal": operation.get("proposal"),
                    }],
                    literal_index=literal_index,
                )
                item = (result["applied"] or result["skipped"])[0]
            elif action == "REMOVE":
                item = _remove_lesson(
                    out_packet,
                    target,
                    lesson_id,
                )
            else:
                item = {"skip_reason": f"unsupported correction operation {action!r}"}
            item["operation_index"] = position
            item["target"] = operation.get("target")
            (applied if "lesson_id" in item else skipped).append(item)

    _write_packet_changelog(out_packet, applied)
    return {
        "packet": str(out_packet),
        "decision": decision,
        "applied": applied,
        "skipped": skipped,
        "lessons_after": lessons_snapshot(out_packet),
    }


def _remove_lesson(packet: Path, target: str, lesson_id: str) -> dict[str, Any]:
    if target not in TARGETS:
        return {"skip_reason": f"unknown or non-writable target {target!r}"}
    if not lesson_id:
        return {"skip_reason": "missing target_lesson_id for REMOVE"}
    section = TARGETS[target]
    path = packet / section.path
    lines = _lines(path)
    lessons = _read_section(lines, section)
    existing = next((lesson for lesson in lessons if lesson.lesson_id == lesson_id), None)
    if existing is None:
        return {"skip_reason": f"target_lesson_id {lesson_id!r} does not exist in {target}"}
    del lines[existing.line]
    remaining = _read_section(lines, section)
    if not remaining:
        lines.insert(_insert_at(lines, section, remaining), "No learned lessons yet.")
    _write(path, lines)
    return {"decision": "REMOVE", "mode": "remove", "lesson_id": lesson_id, "path": section.path}

def _apply_one(packet: Path, decision: dict[str, Any], index: dict[str, Any] | None) -> dict[str, Any]:
    """Apply one decision, returning either a written record or a skip reason."""
    action = str(decision.get("decision") or decision.get("action") or "").upper()
    target = str(decision.get("target") or "")
    proposal = str(decision.get("proposal") or decision.get("content") or "").strip()

    if action == "SKIP":
        return {"skip_reason": "curator chose SKIP"}
    if action not in _WRITES:
        return {"skip_reason": f"unsupported decision {action!r}; use MODIFY or SKIP"}
    if target not in TARGETS:
        return {"skip_reason": f"unknown or non-writable target {target!r}"}
    if not proposal:
        return {"skip_reason": "empty proposal"}

    lint_reasons = lint_lesson_text(proposal, index)
    if lint_reasons:
        return {"skip_reason": "blocked by packet lint", "lint_reasons": lint_reasons}

    section = TARGETS[target]
    path = packet / section.path
    lines = _lines(path)
    lessons = _read_section(lines, section)
    requested = str(decision.get("target_lesson_id") or decision.get("lesson_id") or "").strip()

    if requested:
        existing = next((lesson for lesson in lessons if lesson.lesson_id == requested), None)
        if existing is None:
            return {"skip_reason": f"target_lesson_id {requested!r} does not exist in {target}"}
        lines[existing.line] = f"- [{requested}] {proposal}"
        _write(path, lines)
        return {"decision": "MODIFY", "mode": "replace", "lesson_id": requested, "path": section.path}

    if len(lessons) >= section.capacity:
        return {"skip_reason": f"{target} is at capacity {section.capacity}; "
                               "supply target_lesson_id to replace a lesson"}

    lesson_id = f"{section.slug}#{_next_number(lessons)}"
    lines.insert(_insert_at(lines, section, lessons), f"- [{lesson_id}] {proposal}")
    _write(path, lines)
    return {"decision": "MODIFY", "mode": "append", "lesson_id": lesson_id, "path": section.path}


def _next_number(lessons: list[Lesson]) -> int:
    tails = [lesson.lesson_id.partition("#")[2] for lesson in lessons]
    return max((int(tail) for tail in tails if tail.isdigit()), default=0) + 1


def _insert_at(lines: list[str], section: Section, lessons: list[Lesson]) -> int:
    """After the last existing lesson, or at the end of an empty section."""
    if lessons:
        return lessons[-1].line + 1
    start, end = _bounds(lines, section.heading)
    while end > start and not lines[end - 1].strip():
        end -= 1
    for index in range(end - 1, start - 1, -1):
        if lines[index].strip().lower() == "no learned lessons yet.":
            del lines[index]
            end -= 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return end


def _write_packet_changelog(packet: Path, applied: list[dict[str, Any]]) -> None:
    """Provenance that ships with the packet: ids and targets only.

    Anything written here reaches the agent, so it must never carry evidence,
    rationale, or sample identity.
    """
    changelog = packet / "CHANGELOG.md"
    previous = changelog.read_text(encoding="utf-8", errors="replace") if changelog.exists() else "# Skill Packet Changelog\n"
    redacted = [
        {key: item.get(key) for key in ("decision", "mode", "target", "lesson_id")}
        for item in applied
    ]
    changelog.write_text(
        previous.rstrip() + "\n\n" + json.dumps({"applied": redacted}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- markdown


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bounds(lines: list[str], heading: str) -> tuple[int, int]:
    """[start, end) of a section body, excluding its heading. Empty when absent."""
    try:
        start = lines.index(heading)
    except ValueError:
        return 0, 0
    level = len(_HEADING_RE.match(heading).group(1))
    for index in range(start + 1, len(lines)):
        nested = _HEADING_RE.match(lines[index])
        if nested and len(nested.group(1)) <= level:
            return start + 1, index
    return start + 1, len(lines)


def _bullets(lines: list[str], bounds: tuple[int, int]) -> Iterator[tuple[int, str]]:
    for index in range(*bounds):
        match = _BULLET_RE.match(lines[index])
        if match:
            yield index, match.group(1)


def _read_section(lines: list[str], section: Section) -> list[Lesson]:
    lessons: list[Lesson] = []
    for index, _text in _bullets(lines, _bounds(lines, section.heading)):
        match = _LESSON_RE.match(lines[index])
        if match:
            lessons.append(Lesson(index, match.group(1), match.group(2).strip()))
    return lessons
