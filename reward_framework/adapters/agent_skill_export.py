"""Export two-layer PoC skill packets into native Agent Skill folders."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Iterable


LEVEL_SPECS = (
    {
        "source_dir": "level1_submission_verification",
        "native_name": "poc-submission-verification",
        "description": (
            "Use when reproducing a vulnerability or bug from an issue description "
            "and the agent needs disciplined PoC submission/verification behavior: "
            "meaningful evidence-bearing submits, candidate history, duplicate "
            "avoidance, and analysis-as-diagnostic rather than a runtime gate."
        ),
    },
    {
        "source_dir": "level2_vulnerability_reproduction",
        "native_name": "poc-vulnerability-reproduction",
        "description": (
            "Use when constructing PoC inputs for vulnerability or bug reproduction "
            "from issue description and code evidence, including parser/admission, "
            "source, root cause, sink, trigger reasoning and candidate-plan repair."
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
    prelude = (
        f"\n\nAdapter: `{adapter_name}` native skill export. This skill is one layer of "
        "the reward-framework PoC reproduction packet. If helper scripts are needed, "
        "use the `helpers/` directory inside this skill folder or copy those helpers "
        "into the benchmark workspace.\n\n"
    )
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {_yaml_single_quote(description)}\n"
        "---\n\n"
        + prelude
        + body
    )


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


def export_native_agent_skills(
    packet: Path,
    destination: Path,
    *,
    adapter_name: str,
    overwrite: bool = True,
) -> dict:
    """Export Level 1 and Level 2 packet folders as native Agent Skills."""
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
        skill_text = _native_skill_text(
            spec["native_name"],
            spec["description"],
            (src / "SKILL.md").read_text(encoding="utf-8", errors="replace"),
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
        exported.append(
            {
                "source": str(src),
                "name": spec["native_name"],
                "path": str(dst),
                "helpers": helper_files,
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
        "Use `poc-vulnerability-reproduction` to plan issue-aligned PoC candidates, "
        "then use `poc-submission-verification` to decide and record meaningful "
        "submits. Do not treat training reasoning/reachability diagnostics as "
        "test-time oracle feedback.\n",
        encoding="utf-8",
    )
