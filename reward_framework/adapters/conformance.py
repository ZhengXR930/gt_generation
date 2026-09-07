"""One conformance contract every adapter must satisfy.

A distilled packet is only comparable across harnesses if the same text reaches
the agent everywhere. Adapters legitimately differ in *how* they install it -
OpenHands copies it into the workspace, Codex and Claude export native Agent
Skills, DeepSeek Harness builds a bundle - so the check is written against what
an adapter reports in ``agent_paths`` rather than against any one layout.

Two rules carry the weight:

- the installed SKILL.md must equal the source modulo path substitution, so no
  adapter can quietly edit, truncate, or re-order a lesson;
- the benchmark's ``submit.sh`` must be the benchmark's own. An adapter that
  wraps it changes the exit codes and gating the agent observes, which makes
  its arm incomparable both with the other harnesses and with the
  no-packet baseline in ``poc_generation``.
"""
from __future__ import annotations

import re
from pathlib import Path

from reward_framework.adapters.base import SKILL_PATH_PLACEHOLDERS

SKILL_FILES = ("reproduction_skill/SKILL.md", "submission_skill/SKILL.md")
NATIVE_SKILL_DIRS = {"reproduction_skill": "poc-reproduction", "submission_skill": "poc-submission"}
REQUIRED_HELPERS = ("submit_history.py", "submit_preflight.py")

_PLACEHOLDER_RE = re.compile(r"\$\{[A-Z_]+\}")
_WRAPPER_MARKERS = ("cybergym_submit", "poc_skill_state", "force_finalization_reason")


def check_installed_packet(source_packet: Path, agent_paths: dict[str, str], *, workspace: Path) -> list[str]:
    """Return the ways this installation breaks packet transfer (empty = fine)."""
    return [
        *_check_skills(source_packet, Path(agent_paths.get("skill_packet") or "")),
        *_check_helpers(Path(agent_paths.get("helpers") or "")),
        *_check_submit_is_untouched(workspace),
    ]


def _check_skills(source_packet: Path, installed_root: Path) -> list[str]:
    errors: list[str] = []
    if not installed_root.is_dir():
        return [f"agent_paths.skill_packet is not a directory: {installed_root}"]

    for relative in SKILL_FILES:
        source = source_packet / relative
        installed = _find_installed_skill(installed_root, relative)
        if installed is None:
            errors.append(f"installed packet is missing {relative}")
            continue
        text = installed.read_text(encoding="utf-8", errors="replace")
        leftover = _PLACEHOLDER_RE.findall(text)
        if leftover:
            errors.append(f"{relative} still contains unresolved placeholders: {sorted(set(leftover))}")
        source_text = _substitute_placeholders_for_compare(
            source.read_text(encoding="utf-8", errors="replace"),
            installed_text=text,
        )
        if not _same_lessons(source_text, text):
            errors.append(f"{relative} content differs from the source packet beyond path substitution")
    return errors


def _find_installed_skill(root: Path, relative: str) -> Path | None:
    """Accept either the packet layout or a native Agent Skill export."""
    direct = root / relative
    if direct.is_file():
        return direct
    native = root / NATIVE_SKILL_DIRS[relative.split("/", 1)[0]] / "SKILL.md"
    return native if native.is_file() else None


def _same_lessons(source_text: str, installed_text: str) -> bool:
    """Compare every bullet, which is where the skill's instructions live.

    Native exports legitimately add YAML frontmatter and an adapter prelude, and
    every adapter substitutes different absolute paths, so neither the whole file
    nor a naive line diff is the right comparison. Bullets cover both the learned
    lessons and the fixed R.A / S.A loops, so an adapter cannot quietly edit
    either one. A packet straight from ``skill_packets/initial`` has no lesson
    ids yet, so this must not depend on them.
    """
    return bullet_lines(source_text) == bullet_lines(installed_text)


def _substitute_placeholders_for_compare(source_text: str, *, installed_text: str) -> str:
    path_values = sorted(set(re.findall(r"/[^\s`]+", installed_text)), key=len, reverse=True)
    for placeholder in SKILL_PATH_PLACEHOLDERS:
        replacement = next((value for value in path_values if value.endswith(_placeholder_suffix(placeholder))), "<path>")
        source_text = source_text.replace(placeholder, replacement)
    return source_text


def _placeholder_suffix(placeholder: str) -> str:
    if placeholder == "${HELPERS_DIR}":
        return "helpers"
    if placeholder == "${STATE_DIR}":
        return "state"
    return "workspace"


def bullet_lines(text: str) -> list[str]:
    """Instruction lines in a skill document, with paths neutralised."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") or re.match(r"^\d+\.\s", stripped):
            lines.append(_PLACEHOLDER_RE.sub("<path>", stripped))
    return lines


def _check_helpers(helpers_dir: Path) -> list[str]:
    if not helpers_dir.is_dir():
        return [f"agent_paths.helpers is not a directory: {helpers_dir}"]
    return [
        f"missing helper {name} under {helpers_dir}"
        for name in REQUIRED_HELPERS
        if not (helpers_dir / name).is_file()
    ]


def _check_submit_is_untouched(workspace: Path) -> list[str]:
    submit = workspace / "submit.sh"
    if not submit.is_file():
        return []  # some local runners stage the target differently; nothing to compare
    text = submit.read_text(encoding="utf-8", errors="replace")
    hits = [marker for marker in _WRAPPER_MARKERS if marker in text]
    if hits:
        return [f"submit.sh has been wrapped by the adapter ({', '.join(hits)}); "
                "the agent would observe different exit codes than every other arm"]
    return []
