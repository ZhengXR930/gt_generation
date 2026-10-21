"""Export two-layer PoC skill packets into native Agent Skill folders."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Iterable

from reward_framework.distillation.skill_packet import TARGETS, read_lessons


LEVEL_SPECS = (
    {
        "source_dir": "reproduction_skill",
        "native_name": "poc-reproduction",
        "description": (
            "Use when constructing a concrete proof-of-concept input from the "
            "local issue description, workspace, and submission feedback."
        ),
    },
    {
        "source_dir": "submission_skill",
        "native_name": "poc-submission",
        "description": (
            "Use when submitting a concrete local PoC candidate and recording "
            "the returned benchmark feedback for later revision."
        ),
    },
)

FORBIDDEN_TEXT = (
    ".gt_skill_state",
    "Static GT PoC",
    "GT trace",
    "GT feedback",
    "aim for at least three effective submits",
    "schema gate",
)


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[end + len("\n---\n") :].lstrip()
    return text


def _yaml_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _native_skill_text(name: str, description: str, body: str, adapter_name: str) -> str:
    body = _strip_frontmatter(body)
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {_yaml_single_quote(description)}\n"
        "---\n\n"
        + body
    )


def _inline_modular_lessons(packet: Path, source_dir: str, body: str) -> tuple[str, bool]:
    """Compile a modular source packet into the first-read agent skill text.

    Lesson files are the editable source of truth for the distillation loop, but
    DSH (and other native-skill runtimes) inject only ``SKILL.md`` at skill-load
    time.  Leaving the rules only in child files therefore makes their delivery
    depend on an extra model-initiated file read.  Export the same lesson bodies
    inline so the agent receives the exact instruction set that a legacy flat
    packet delivered.  The source resources remain copied for auditability and
    future editing; they are not the delivery mechanism.
    """
    target = next(
        (name for name, section in TARGETS.items()
         if Path(section.path).parent.as_posix() == source_dir),
        None,
    )
    if target is None:
        return body, False
    section = TARGETS[target]
    if not (packet / section.lesson_dir / "INDEX.md").is_file():
        return body, False

    lessons = read_lessons(packet)[target]
    rendered = [section.heading, ""]
    rendered.extend(f"- [{lesson['lesson_id']}] {lesson['text']}" for lesson in lessons)
    replacement = "\n".join(rendered)
    pattern = re.compile(
        r"^## Learned lesson routing\n.*?(?=^## |\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    compiled, substitutions = pattern.subn(replacement + "\n", body, count=1)
    if substitutions != 1:
        raise ValueError(
            f"modular skill {source_dir} has lessons but no unique learned-lesson router to compile"
        )
    return compiled, True


def compile_modular_lessons_for_agent(packet: Path) -> dict[str, bool]:
    """Compile modular learned rules into an agent-facing *copy* of a packet.

    Call this only after a harness has copied the immutable source packet into
    its own workspace.  The source packet remains modular for curator edits;
    the copy receives all currently active rules at first skill load.
    """
    packet = packet.resolve()
    compiled: dict[str, bool] = {}
    for spec in LEVEL_SPECS:
        skill = packet / spec["source_dir"] / "SKILL.md"
        body = skill.read_text(encoding="utf-8", errors="replace")
        agent_body, changed = _inline_modular_lessons(packet, spec["source_dir"], body)
        if changed:
            skill.write_text(agent_body, encoding="utf-8")
        compiled[spec["source_dir"]] = changed
    return compiled


def _validate_source_packet(packet: Path) -> None:
    if not packet.is_dir():
        raise FileNotFoundError(f"missing skill packet: {packet}")
    for spec in LEVEL_SPECS:
        skill = packet / spec["source_dir"] / "SKILL.md"
        if not skill.is_file():
            raise FileNotFoundError(f"missing source SKILL.md: {skill}")
        text = skill.read_text(encoding="utf-8", errors="replace")
        for bad in FORBIDDEN_TEXT:
            if bad in text:
                raise ValueError(f"{skill} contains forbidden stale text {bad!r}")
        lessons = skill.parent / "lessons"
        if lessons.exists() and not (lessons / "INDEX.md").is_file():
            raise FileNotFoundError(f"modular skill is missing lesson index: {lessons / 'INDEX.md'}")
        for resource in lessons.rglob("*.md") if lessons.is_dir() else ():
            resource_text = resource.read_text(encoding="utf-8", errors="replace")
            for bad in FORBIDDEN_TEXT:
                if bad in resource_text:
                    raise ValueError(f"{resource} contains forbidden stale text {bad!r}")


def export_native_agent_skills(
    packet: Path,
    destination: Path,
    *,
    adapter_name: str,
    overwrite: bool = True,
) -> dict:
    """Export the Reproduction and Submission packet folders as native Agent Skills."""
    packet = packet.resolve()
    destination = destination.expanduser().resolve()
    _validate_source_packet(packet)
    destination.mkdir(parents=True, exist_ok=True)

    exported: list[dict] = []
    for spec in LEVEL_SPECS:
        src = packet / spec["source_dir"]
        dst = destination / spec["native_name"]
        if dst.exists():
            if not overwrite:
                raise FileExistsError(f"native skill already exists: {dst}")
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        source_body = (src / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        agent_body, lessons_inlined = _inline_modular_lessons(packet, spec["source_dir"], source_body)
        skill_text = _native_skill_text(
            spec["native_name"],
            spec["description"],
            agent_body,
            adapter_name,
        )
        (dst / "SKILL.md").write_text(skill_text, encoding="utf-8")
        helpers = src / "helpers"
        helper_files: list[str] = []
        if helpers.is_dir():
            ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
            shutil.copytree(helpers, dst / "helpers", ignore=ignore)
            shutil.copytree(helpers, dst / "scripts", ignore=ignore)
            helper_files = sorted(p.name for p in helpers.glob("*.py"))
        lessons = src / "lessons"
        lesson_files: list[str] = []
        if lessons.is_dir():
            ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
            shutil.copytree(lessons, dst / "lessons", ignore=ignore)
            lesson_files = sorted(str(path.relative_to(lessons)) for path in lessons.rglob("*.md"))
        exported.append(
            {
                "source": str(src),
                "name": spec["native_name"],
                "path": str(dst),
                "helpers": helper_files,
                "lessons": lesson_files,
                "lesson_delivery": "inline-materialized" if lessons_inlined else "source-skill",
            }
        )

    manifest = {
        "adapter": adapter_name,
        "format": "native-agent-skills",
        "source_packet": str(packet),
        "destination": str(destination),
        "skills": exported,
    }
    (destination / "reward_framework_skill_export.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def validate_native_agent_skills(destination: Path) -> dict:
    """Validate exported native skills without assuming a specific agent runtime."""
    destination = destination.expanduser().resolve()
    if not destination.is_dir():
        raise FileNotFoundError(f"missing native skills destination: {destination}")
    found: list[dict] = []
    for spec in LEVEL_SPECS:
        skill_dir = destination / spec["native_name"]
        skill = skill_dir / "SKILL.md"
        if not skill.is_file():
            raise FileNotFoundError(f"missing native skill: {skill}")
        text = skill.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---\n"):
            raise ValueError(f"native skill missing YAML frontmatter: {skill}")
        if f"name: {spec['native_name']}" not in text:
            raise ValueError(f"native skill has wrong name: {skill}")
        if "description:" not in text:
            raise ValueError(f"native skill missing description: {skill}")
        for bad in FORBIDDEN_TEXT:
            if bad in text:
                raise ValueError(f"{skill} contains forbidden stale text {bad!r}")
        for helper in (skill_dir / "helpers").glob("*.py"):
            compile(helper.read_text(encoding="utf-8", errors="replace"), str(helper), "exec")
        lessons = skill_dir / "lessons"
        if lessons.exists() and not (lessons / "INDEX.md").is_file():
            raise FileNotFoundError(f"native skill is missing lesson index: {lessons / 'INDEX.md'}")
        for stale in skill_dir.rglob("__pycache__"):
            raise ValueError(f"native skill export contains stale __pycache__: {stale}")
        for stale in skill_dir.rglob("*.pyc"):
            raise ValueError(f"native skill export contains stale pyc: {stale}")
        found.append({"name": spec["native_name"], "path": str(skill_dir)})
    return {"status": "pass", "destination": str(destination), "skills": found}


def write_bridge_file(path: Path, *, adapter_name: str, skills_dir: Path) -> None:
    """Write a small project-level bridge document without mutating native config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Reward Framework PoC Skills\n\n"
        f"Adapter: `{adapter_name}`\n\n"
        f"Native skills directory: `{skills_dir}`\n\n"
        "Installed skills: `poc-reproduction`, `poc-submission`.\n\n"
        "Before beginning vulnerability analysis, read both installed skill files:\n\n"
        f"- `{skills_dir}/poc-reproduction/SKILL.md`\n"
        f"- `{skills_dir}/poc-submission/SKILL.md`\n\n"
        "Use their reproduction and submission loops throughout this task.\n",
        encoding="utf-8",
    )
