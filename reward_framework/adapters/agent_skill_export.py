"""Export legacy or five-stage PoC skill packets into native Agent Skill folders."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from reward_framework.distillation.skill_packet import (
    is_direct_stage_packet,
    is_stage_packet,
    read_lessons,
    targets_for_packet,
)


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

STAGE_SPECS = tuple(
    {
        "source_dir": f"skills/{stage}",
        "native_name": f"poc-{stage.replace('_', '-')}",
        "description": f"Use when working on the {stage.replace('_', ' ')} stage of PoC reproduction.",
    }
    for stage in (
        "construct_input",
        "reach_vulnerable_code",
        "trigger_condition",
        "verify_result",
        "iterate",
    )
)

# A stage packet is split into five files for the distillation loop, not into
# five independent agent actions.  Native skill runtimes discover each
# immediate child of their skill root as a separate callable skill, so expose
# one lifecycle skill to the agent and keep the five source files as the
# editable/auditable packet.
STAGE_BUNDLE_SPEC = {
    "source_dir": "skills",
    "native_name": "poc-lifecycle",
    "description": "Use the complete PoC reproduction stage scaffold.",
}


def packet_specs(packet: Path) -> tuple[dict[str, str], ...]:
    """Return the source skill layout for a legacy or five-stage packet."""
    return STAGE_SPECS if is_stage_packet(packet) else LEVEL_SPECS


def native_packet_specs(packet: Path) -> tuple[dict[str, str], ...]:
    """Return the skills actually exposed to a native agent runtime.

    Five stage files remain the source layout used by Teacher/Curator.  They
    are deliberately compiled into one agent-facing skill so loading the
    initial scaffold does not cause one model tool call per empty stage.
    """
    return (STAGE_BUNDLE_SPEC,) if is_stage_packet(packet) else LEVEL_SPECS


def packet_helper_spec(packet: Path) -> dict[str, str] | None:
    """Return the source and native helper location for a packet.

    The legacy two-layer packet carries submission bookkeeping helpers.  The
    five-stage packet is intentionally just its five stage ``SKILL.md`` files;
    its harness records submissions independently and therefore has no
    agent-facing helper skill.
    """
    if is_stage_packet(packet) or is_direct_stage_packet(packet):
        return None
    return {"source": "submission_skill/helpers", "native_name": "poc-submission"}


def inject_stage_skills_into_prompt(packet: Path, env: dict[str, str], output_dir: Path) -> str | None:
    """Append every five-stage skill to the task prompt before the agent starts.

    Native skill directories remain installed for filesystem provenance, but the
    active stage packet is deliberately delivered eagerly: the coding agent
    receives all five Goal sections and learned rules in its initial context,
    rather than relying on a later skill-tool read.
    """
    packet = packet.resolve()
    if not is_stage_packet(packet):
        return None
    source = Path(str(env.get("HARNESS_TASK_PROMPT_FILE") or "")).expanduser()
    if not source.is_file():
        return None
    body, _ = stage_bundle_body(packet)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "reward_framework_prompt_with_stages.txt"
    prompt = source.read_text(encoding="utf-8", errors="replace").rstrip()
    prompt += "\n\n" + body
    destination.write_text(prompt, encoding="utf-8")
    env["HARNESS_TASK_PROMPT_FILE"] = str(destination)
    return str(destination)

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


def stage_bundle_body(packet: Path) -> tuple[str, bool]:
    """Return the five stage documents as one agent-facing body.

    The body is made only from the source ``SKILL.md`` files.  No routing
    instruction or behavioral lesson is synthesized here; stage headings and
    learned lessons stay exactly where Teacher/Curator placed them.
    """
    sections: list[str] = []
    lessons_inlined = False
    for spec in STAGE_SPECS:
        source = packet / spec["source_dir"] / "SKILL.md"
        body = source.read_text(encoding="utf-8", errors="replace")
        body, changed = _inline_modular_lessons(packet, spec["source_dir"], body)
        lessons_inlined = lessons_inlined or changed
        sections.append(_strip_frontmatter(body).strip())
    return "\n\n---\n\n".join(sections) + "\n", lessons_inlined


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
        (name for name, section in targets_for_packet(packet).items()
         if Path(section.path).parent.as_posix() == source_dir),
        None,
    )
    if target is None:
        return body, False
    section = targets_for_packet(packet)[target]
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
    for spec in packet_specs(packet):
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
    if is_stage_packet(packet) and (packet / "helpers").exists():
        raise ValueError("five-stage packets must contain only stage skills, not agent-facing helpers")
    specs = packet_specs(packet)
    for spec in specs:
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
    """Export a packet in the native layout expected by its agent runtime."""
    packet = packet.resolve()
    destination = destination.expanduser().resolve()
    _validate_source_packet(packet)
    destination.mkdir(parents=True, exist_ok=True)

    exported: list[dict] = []
    specs = native_packet_specs(packet)
    helper_spec = packet_helper_spec(packet)
    for spec in specs:
        src = packet / spec["source_dir"]
        dst = destination / spec["native_name"]
        if dst.exists():
            if not overwrite:
                raise FileExistsError(f"native skill already exists: {dst}")
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        if is_stage_packet(packet):
            source_body, lessons_inlined = stage_bundle_body(packet)
        else:
            source_body = (src / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            source_body, lessons_inlined = _inline_modular_lessons(
                packet, spec["source_dir"], source_body
            )
        skill_text = _native_skill_text(
            spec["native_name"],
            spec["description"],
            source_body,
            adapter_name,
        )
        (dst / "SKILL.md").write_text(skill_text, encoding="utf-8")
        helpers = src / "helpers" if helper_spec else None
        helper_files: list[str] = []
        if helpers and helpers.is_dir() and helper_spec and spec["native_name"] == helper_spec["native_name"]:
            ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
            shutil.copytree(helpers, dst / "helpers", ignore=ignore)
            shutil.copytree(helpers, dst / "scripts", ignore=ignore)
            helper_files = sorted(p.name for p in helpers.glob("*.py"))
        # Stage lesson resources are compiled into the one lifecycle skill;
        # copying them as immediate child skill resources would reintroduce
        # per-stage on-demand loading in some native runtimes.
        lessons = src / "lessons" if not is_stage_packet(packet) else None
        lesson_files: list[str] = []
        if lessons is not None and lessons.is_dir():
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
        "agent_delivery": "eager-aggregate" if is_stage_packet(packet) else "native-per-skill",
        "skills": exported,
        "source_stage_skills": [
            {
                "source": str(packet / spec["source_dir"] / "SKILL.md"),
                "name": spec["native_name"],
            }
            for spec in STAGE_SPECS
        ] if is_stage_packet(packet) else [],
        "helper_skill": helper_spec["native_name"] if helper_spec else None,
        "helper_source": helper_spec["source"] if helper_spec else None,
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
    exported_is_stage = (destination / STAGE_BUNDLE_SPEC["native_name"]).is_dir()
    for spec in ((STAGE_BUNDLE_SPEC,) if exported_is_stage else LEVEL_SPECS):
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
    skill_paths = sorted(
        child / "SKILL.md"
        for child in skills_dir.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    ) if skills_dir.is_dir() else []
    skill_lines = "\n".join(f"- `{item}`" for item in skill_paths)
    if not skill_lines:
        skill_lines = f"- `{skills_dir}/*/SKILL.md`"
    path.write_text(
        "# Reward Framework PoC Skills\n\n"
        f"Adapter: `{adapter_name}`\n\n"
        f"Native skills directory: `{skills_dir}`\n\n"
        "Before beginning vulnerability analysis, read every installed skill file:\n\n"
        f"{skill_lines}\n\n"
        "Use all of the installed stage guidance throughout this task.\n",
        encoding="utf-8",
    )
