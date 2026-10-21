"""Skill packet copy, lesson addressing, and curator-update application.

Lessons carry stable ids so a curator decision can replace an existing lesson
instead of only ever appending one.  Packets predating the modular layout keep
their ``- [R.B#3] ...`` entries in ``SKILL.md`` and remain readable.  New
packets may put each learned rule in ``lessons/<condition>-<purpose>.md`` while
``SKILL.md`` remains the stable routing framework.  This keeps learned content
separate without turning every lesson into a separate native agent skill.

Only proposal text reaches agent-visible packet resources.  Evidence stays in
run artifacts, never in the packet.
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
    """One writable lesson collection attached to a stable ``SKILL.md``."""

    path: str
    heading: str
    capacity: int
    lesson_dir: str

    @property
    def slug(self) -> str:
        """The id prefix for lessons in this section, e.g. ``R.B``."""
        return self.heading.split()[1]


TARGETS: dict[str, Section] = {
    "reproduction:R.B": Section(
        "reproduction_skill/SKILL.md", "## R.B Learned Reproduction Lessons", 12,
        "reproduction_skill/lessons",
    ),
    "submission:S.C": Section(
        "submission_skill/SKILL.md", "## S.C Learned Submission Lessons", 8,
        "submission_skill/lessons",
    ),
}

# The curator vocabulary is MODIFY / REMOVE / SKIP. ACCEPT and MERGE are
# tolerated as legacy spellings of MODIFY rather than rejected as malformed.
_WRITES = {"MODIFY", "ACCEPT", "MERGE"}

_LESSON_RE = re.compile(r"^-\s+\[([A-Za-z0-9.#_-]+)\]\s*(.*)$")
_BULLET_RE = re.compile(r"^-\s+(.*)$")
_HEADING_RE = re.compile(r"^(#+)\s")


class Lesson(NamedTuple):
    line: int
    lesson_id: str
    text: str
    path: str = ""


_MODULE_RE = re.compile(r"\A---\n(?P<header>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_ROUTER = """## Learned lesson routing

Read `lessons/INDEX.md` after loading this skill and immediately before the
first submission. Immediately after every submission response, before any new
source investigation or other action, re-read the index; for every condition
supported by information you can see, read its lesson file and use it to choose
the next action. Do not use unavailable evaluator or ground-truth information
as a condition.
"""

# These descriptive names are deliberately based on signals visible to the
# agent.  They are presentation metadata only: the lesson body is copied
# byte-for-byte from the prior packet so a layout experiment does not alter the
# learned instruction itself.
_KNOWN_MODULE_METADATA = {
    ("reproduction:R.B", "R.B#1"): (
        "candidate runs without a sanitizer report",
        "change a structural precondition",
        "clean-run-change-precondition.md",
    ),
    ("reproduction:R.B", "R.B#2"): (
        "returned crash differs from the issue-stated failure",
        "switch to a focused source-grounded hypothesis",
        "near-miss-crash-switch-hypothesis.md",
    ),
    ("submission:S.C", "S.C#1"): (
        "submission returns only a runner or container error",
        "retry an untested candidate",
        "runner-error-retry-candidate.md",
    ),
}


# --------------------------------------------------------------------------- packets


def copy_initial_packet(out: Path, source: Path = INITIAL_PACKET) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(source, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    normalize_packet(out)
    return out


def normalize_packet(packet: Path) -> None:
    """Normalize writable sections without changing their learned meaning.

    Legacy packets can contain a duplicated section heading or an exact copy of
    a lesson.  The former makes ``_bounds`` see an empty section, while the
    latter makes the agent read the same instruction twice.  Keep the first
    occurrence of an exact lesson, retain distinct lessons, and give every
    retained lesson a unique stable id.
    """
    for section in TARGETS.values():
        if _is_modular(packet, section):
            _normalize_modules(packet, section)
            continue
        path = packet / section.path
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        changed = False

        # A repeated section heading terminates the first section under the
        # Markdown-boundary parser.  There is only one writable section with
        # each of these headings, so retaining the first is unambiguous.
        heading_seen = False
        compacted: list[str] = []
        for line in lines:
            if line == section.heading:
                if heading_seen:
                    changed = True
                    continue
                heading_seen = True
            compacted.append(line)
        lines = compacted

        # Preserve the first exact lesson and drop only byte-content
        # duplicates modulo whitespace.  This is deliberately not semantic
        # deduplication: two differently worded rules may be meaningfully
        # distinct even if they look related.
        duplicate_lines: list[int] = []
        seen_text: set[str] = set()
        for lesson in _read_section(lines, section):
            key = _lesson_key(lesson.text)
            if key in seen_text:
                duplicate_lines.append(lesson.line)
            else:
                seen_text.add(key)
        for line_index in reversed(duplicate_lines):
            del lines[line_index]
            changed = True

        taken: set[str] = set()
        next_number = 1
        for index, text in _bullets(lines, _bounds(lines, section.heading)):
            match = _LESSON_RE.match(lines[index])
            if match and match.group(1) not in taken:
                taken.add(match.group(1))
                continue
            while f"{section.slug}#{next_number}" in taken:
                next_number += 1
            lesson_id = f"{section.slug}#{next_number}"
            taken.add(lesson_id)
            lesson_text = match.group(2).strip() if match else text
            lines[index] = f"- [{lesson_id}] {lesson_text}"
            changed = True
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def materialize_modular_lessons(packet: Path) -> dict[str, list[dict[str, str]]]:
    """Move learned rules into per-lesson Markdown resources.

    The stable skill framework retains its fixed loop and gains a short router.
    Existing lesson bodies are preserved verbatim.  This is intentionally
    explicit rather than an implicit part of ``normalize_packet``: historical
    packets retain their original layout, while a layout experiment gets a
    separately named packet and an auditable conversion point.
    """
    for target, section in TARGETS.items():
        if _is_modular(packet, section):
            _normalize_modules(packet, section)
            continue
        path = packet / section.path
        lines = _lines(path)
        lessons = _read_section(lines, section)
        start, end = _bounds(lines, section.heading)
        if not start and not end:
            raise ValueError(f"cannot materialize lessons: missing {section.heading!r} in {path}")

        lesson_root = packet / section.lesson_dir
        lesson_root.mkdir(parents=True, exist_ok=True)
        for lesson in lessons:
            condition, purpose, filename = _module_metadata(target, lesson.lesson_id, lesson.text)
            _write_module(
                lesson_root / filename,
                lesson_id=lesson.lesson_id,
                target=target,
                condition=condition,
                purpose=purpose,
                text=lesson.text,
            )

        # Replace exactly the old learned section, retaining all fixed material
        # before and after it (including the submission helpers).
        lines[start - 1:end] = _ROUTER.rstrip("\n").splitlines()
        _write(path, lines)
        _write_lesson_index(packet, target, section)
    normalize_packet(packet)
    return read_lessons(packet)


def read_lessons(packet: Path) -> dict[str, list[dict[str, str]]]:
    """Current lessons per writable target, with ids."""
    return {
        target: [
            {"lesson_id": lesson.lesson_id, "text": lesson.text}
            for lesson in _read_target_lessons(packet, section)
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
    if decision not in {"KEEP", "CORRECT", "ROLLBACK"}:
        raise ValueError(f"unsupported correction decision {decision!r}")

    source = previous_packet if decision == "ROLLBACK" else current_packet
    if out_packet.exists():
        shutil.rmtree(out_packet)
    shutil.copytree(source, out_packet, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    normalize_packet(out_packet)

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if decision == "CORRECT":
        operations = correction.get("operations") or []
        if not operations and (correction.get("target") or correction.get("target_lesson_id")):
            operations = [correction]
        for position, operation in enumerate(operations):
            action = str(operation.get("action") or operation.get("decision") or "").upper()
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
    if _is_modular(packet, section):
        lessons = _read_target_lessons(packet, section)
        existing = next((lesson for lesson in lessons if lesson.lesson_id == lesson_id), None)
        if existing is None:
            return {"skip_reason": f"target_lesson_id {lesson_id!r} does not exist in {target}"}
        Path(existing.path).unlink()
        _write_lesson_index(packet, target, section)
        return {
            "decision": "REMOVE",
            "mode": "remove",
            "lesson_id": lesson_id,
            "path": str(Path(section.lesson_dir) / Path(existing.path).name),
        }

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
    operation = str(decision.get("operation") or "").strip().lower()
    requested = str(decision.get("target_lesson_id") or decision.get("lesson_id") or "").strip()
    proposal = str(decision.get("proposal") or decision.get("content") or "").strip()

    # Curator output has both a top-level decision and an edit operation.  A
    # deletion carries no lesson text, so it must be dispatched before the
    # generic empty-proposal guard used by add/revise operations.
    if operation in {"delete", "remove"}:
        action = "REMOVE"

    if action == "SKIP":
        return {"skip_reason": "curator chose SKIP"}
    if action == "REMOVE":
        return _remove_lesson(packet, target, requested)
    if action not in _WRITES:
        return {"skip_reason": f"unsupported decision {action!r}; use MODIFY, REMOVE, or SKIP"}
    if target not in TARGETS:
        return {"skip_reason": f"unknown or non-writable target {target!r}"}
    if not proposal:
        return {"skip_reason": "empty proposal"}

    lint_reasons = lint_lesson_text(proposal, index)
    if lint_reasons:
        return {"skip_reason": "blocked by packet lint", "lint_reasons": lint_reasons}

    section = TARGETS[target]
    path = packet / section.path
    lessons = _read_target_lessons(packet, section)
    if _is_modular(packet, section):
        return _apply_one_to_modules(packet, target, section, lessons, requested, proposal)

    lines = _lines(path)

    if requested:
        existing = next((lesson for lesson in lessons if lesson.lesson_id == requested), None)
        if existing is None:
            return {"skip_reason": f"target_lesson_id {requested!r} does not exist in {target}"}
        if any(
            lesson.lesson_id != requested and _lesson_key(lesson.text) == _lesson_key(proposal)
            for lesson in lessons
        ):
            return {"skip_reason": "proposal duplicates an existing lesson"}
        lines[existing.line] = f"- [{requested}] {proposal}"
        _write(path, lines)
        return {"decision": "MODIFY", "mode": "replace", "lesson_id": requested, "path": section.path}

    if any(_lesson_key(lesson.text) == _lesson_key(proposal) for lesson in lessons):
        return {"skip_reason": "proposal duplicates an existing lesson"}

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


def _lesson_key(text: str) -> str:
    """Stable key for exact lesson deduplication, ignoring formatting only."""
    return " ".join(text.split())


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


# -------------------------------------------------------------------- lesson modules


def _is_modular(packet: Path, section: Section) -> bool:
    """Whether this target uses an explicit ``lessons/`` collection."""
    return (packet / section.lesson_dir / "INDEX.md").is_file()


def _read_target_lessons(packet: Path, section: Section) -> list[Lesson]:
    if not _is_modular(packet, section):
        return _read_section(_lines(packet / section.path), section)
    root = packet / section.lesson_dir
    lessons: list[Lesson] = []
    for path in sorted(root.glob("*.md")):
        if path.name == "INDEX.md":
            continue
        parsed = _read_module(path)
        if parsed is not None:
            lessons.append(parsed)
    return sorted(lessons, key=lambda lesson: (_lesson_number(lesson.lesson_id), lesson.lesson_id))


def _read_module(path: Path) -> Lesson | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = _MODULE_RE.match(text)
    if not match:
        return None
    header = _parse_header(match.group("header"))
    lesson_id = header.get("lesson_id", "").strip()
    if not lesson_id:
        return None
    return Lesson(-1, lesson_id, match.group("body").strip(), str(path))


def _parse_header(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip():
            values[key.strip()] = value.strip()
    return values


def _module_metadata(target: str, lesson_id: str, text: str) -> tuple[str, str, str]:
    known = _KNOWN_MODULE_METADATA.get((target, lesson_id))
    if known is not None:
        return known
    first_sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    words = re.findall(r"[A-Za-z0-9]+", first_sentence.lower())
    condition = " ".join(words[:7]) or "observed candidate feedback"
    purpose_words = re.findall(r"[A-Za-z0-9]+", text.lower())
    purpose = " ".join(purpose_words[-5:]) or "choose the next revision"
    stem = "-".join(words[:7]) or f"lesson-{lesson_id.lower().replace('#', '-') }"
    return condition, purpose, f"{stem[:72].rstrip('-')}.md"


def _write_module(
    path: Path,
    *,
    lesson_id: str,
    target: str,
    condition: str,
    purpose: str,
    text: str,
) -> None:
    path.write_text(
        "---\n"
        f"lesson_id: {lesson_id}\n"
        f"target: {target}\n"
        f"condition: {condition}\n"
        f"purpose: {purpose}\n"
        "---\n"
        f"{text.strip()}\n",
        encoding="utf-8",
    )


def _write_lesson_index(packet: Path, target: str, section: Section) -> None:
    root = packet / section.lesson_dir
    root.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[Lesson, dict[str, str], Path]] = []
    for path in sorted(root.glob("*.md")):
        if path.name == "INDEX.md":
            continue
        parsed = _read_module(path)
        if parsed is None:
            continue
        match = _MODULE_RE.match(path.read_text(encoding="utf-8", errors="replace"))
        assert match is not None
        entries.append((parsed, _parse_header(match.group("header")), path))
    entries.sort(key=lambda item: (_lesson_number(item[0].lesson_id), item[0].lesson_id))

    lines = [
        "# Learned lesson index",
        "",
        "Read a lesson only when its **Use when** condition is supported by information you can see in the issue, workspace, your candidate, or returned submission feedback.",
        "",
    ]
    if not entries:
        lines.append("No learned lessons yet.")
    else:
        for lesson, header, path in entries:
            lines.extend([
                f"## {lesson.lesson_id}",
                "",
                f"**Use when:** {header.get('condition', 'the condition described by this lesson')}",
                f"**Purpose:** {header.get('purpose', 'choose the next action')}",
                f"**Read:** `{path.name}`",
                "",
            ])
    (root / "INDEX.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _normalize_modules(packet: Path, section: Section) -> None:
    root = packet / section.lesson_dir
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    for path in sorted(root.glob("*.md")):
        if path.name == "INDEX.md":
            continue
        lesson = _read_module(path)
        if lesson is None or lesson.lesson_id in seen_ids or _lesson_key(lesson.text) in seen_text:
            path.unlink()
            continue
        seen_ids.add(lesson.lesson_id)
        seen_text.add(_lesson_key(lesson.text))
    target = next(name for name, candidate in TARGETS.items() if candidate == section)
    _write_lesson_index(packet, target, section)


def _module_path_for(
    packet: Path,
    section: Section,
    filename: str,
    *,
    exclude: Path | None = None,
) -> Path:
    root = packet / section.lesson_dir
    candidate = root / filename
    number = 2
    while candidate.exists() and candidate != exclude:
        candidate = root / f"{Path(filename).stem}-{number}{Path(filename).suffix or '.md'}"
        number += 1
    return candidate


def _apply_one_to_modules(
    packet: Path,
    target: str,
    section: Section,
    lessons: list[Lesson],
    requested: str,
    proposal: str,
) -> dict[str, Any]:
    if requested:
        existing = next((lesson for lesson in lessons if lesson.lesson_id == requested), None)
        if existing is None:
            return {"skip_reason": f"target_lesson_id {requested!r} does not exist in {target}"}
        if any(lesson.lesson_id != requested and _lesson_key(lesson.text) == _lesson_key(proposal)
               for lesson in lessons):
            return {"skip_reason": "proposal duplicates an existing lesson"}
        old_path = Path(existing.path)
        condition, purpose, filename = _module_metadata(target, requested, proposal)
        new_path = _module_path_for(packet, section, filename, exclude=old_path)
        _write_module(new_path, lesson_id=requested, target=target, condition=condition, purpose=purpose, text=proposal)
        if new_path != old_path:
            old_path.unlink()
        _write_lesson_index(packet, target, section)
        return {
            "decision": "MODIFY",
            "mode": "replace",
            "lesson_id": requested,
            "path": str(Path(section.lesson_dir) / new_path.name),
        }

    if any(_lesson_key(lesson.text) == _lesson_key(proposal) for lesson in lessons):
        return {"skip_reason": "proposal duplicates an existing lesson"}
    if len(lessons) >= section.capacity:
        return {"skip_reason": f"{target} is at capacity {section.capacity}; "
                               "supply target_lesson_id to replace a lesson"}

    lesson_id = f"{section.slug}#{_next_number(lessons)}"
    condition, purpose, filename = _module_metadata(target, lesson_id, proposal)
    new_path = _module_path_for(packet, section, filename)
    _write_module(new_path, lesson_id=lesson_id, target=target, condition=condition, purpose=purpose, text=proposal)
    _write_lesson_index(packet, target, section)
    return {
        "decision": "MODIFY",
        "mode": "append",
        "lesson_id": lesson_id,
        "path": str(Path(section.lesson_dir) / new_path.name),
    }


def _lesson_number(lesson_id: str) -> int:
    tail = lesson_id.partition("#")[2]
    return int(tail) if tail.isdigit() else 10**9


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
