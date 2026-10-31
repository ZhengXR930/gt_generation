"""One conformance contract every adapter must satisfy.

A distilled packet is only comparable across harnesses if the same text reaches
the agent everywhere. Adapters legitimately differ in *how* they install it -
OpenHands copies it into the workspace, Codex and Claude export native Agent
Skills, DeepSeek Harness builds a bundle - so the check is written against what
an adapter reports in ``agent_paths`` rather than against any one layout.

Two rules carry the weight:

- the installed skill framework and every learned lesson resource must equal
  the source modulo path substitution, so no adapter can quietly edit,
  truncate, or re-order a lesson;
- the benchmark's ``submit.sh`` must be the benchmark's own. An adapter that
  wraps it changes the exit codes and gating the agent observes, which makes
  its arm incomparable both with the other harnesses and with the
  no-packet baseline in ``poc_generation``.
"""
from __future__ import annotations

import re
from pathlib import Path

from reward_framework.adapters.base import SKILL_PATH_PLACEHOLDERS
from reward_framework.adapters.agent_skill_export import (
    LEVEL_SPECS,
    STAGE_BUNDLE_SPEC,
    STAGE_SPECS,
    _strip_frontmatter,
    stage_bundle_body,
)
from reward_framework.distillation.skill_packet import (
    is_direct_stage_packet,
    is_stage_packet,
    read_lessons,
    targets_for_packet,
)

REQUIRED_HELPERS = ("submit_history.py", "submit_preflight.py")

_PLACEHOLDER_RE = re.compile(r"\$\{[A-Z_]+\}")
_WRAPPER_MARKERS = ("cybergym_submit", "poc_skill_state", "force_finalization_reason")


def check_installed_packet(source_packet: Path, agent_paths: dict[str, str], *, workspace: Path) -> list[str]:
    """Return the ways this installation breaks packet transfer (empty = fine)."""
    return [
        *_check_skills(source_packet, Path(agent_paths.get("skill_packet") or "")),
        *_check_helpers(source_packet, Path(agent_paths.get("helpers") or "")),
        *_check_submit_is_untouched(workspace),
    ]


def _check_skills(source_packet: Path, installed_root: Path) -> list[str]:
    errors: list[str] = []
    if not installed_root.is_dir():
        return [f"agent_paths.skill_packet is not a directory: {installed_root}"]

    if is_stage_packet(source_packet):
        installed = installed_root / STAGE_BUNDLE_SPEC["native_name"] / "SKILL.md"
        if not installed.is_file():
            return ["installed packet is missing the aggregated five-stage lifecycle skill"]
        installed_text = installed.read_text(encoding="utf-8", errors="replace")
        expected_text, _ = stage_bundle_body(source_packet)
        expected_text = _substitute_placeholders_for_compare(
            expected_text,
            installed_text=_strip_frontmatter(installed_text),
        )
        if _strip_frontmatter(installed_text).strip() != expected_text.strip():
            errors.append("aggregated lifecycle skill differs from the five source stages")
        return errors

    specs = STAGE_SPECS if is_stage_packet(source_packet) else LEVEL_SPECS
    for spec in specs:
        relative = f"{spec['source_dir']}/SKILL.md"
        source = source_packet / relative
        installed = _find_installed_skill(installed_root, spec["native_name"], relative)
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
        if not _same_lessons(source_packet, relative, source_text, text):
            errors.append(f"{relative} content differs from the source packet beyond path substitution")
        errors.extend(_check_lesson_resources(source, installed, relative))
    return errors


def _check_lesson_resources(source_skill: Path, installed_skill: Path, relative: str) -> list[str]:
    """Compare modular lesson resources exactly; they are agent-visible text."""
    source_root = source_skill.parent / "lessons"
    if not source_root.is_dir():
        return []
    installed_root = installed_skill.parent / "lessons"
    if not installed_root.is_dir():
        return [f"installed packet is missing lessons for {relative}"]
    source_files = sorted(path.relative_to(source_root) for path in source_root.rglob("*.md"))
    installed_files = sorted(path.relative_to(installed_root) for path in installed_root.rglob("*.md"))
    if source_files != installed_files:
        return [f"lesson resources differ for {relative}: source={source_files}, installed={installed_files}"]
    errors: list[str] = []
    for child in source_files:
        source_text = (source_root / child).read_text(encoding="utf-8", errors="replace")
        installed_text = (installed_root / child).read_text(encoding="utf-8", errors="replace")
        source_text = _substitute_placeholders_for_compare(source_text, installed_text=installed_text)
        if source_text != installed_text:
            errors.append(f"lesson resource differs from source: {relative.rsplit('/', 1)[0]}/lessons/{child}")
    return errors


def _find_installed_skill(
    root: Path, native_name: str, relative: str | None = None
) -> Path | None:
    """Accept either the packet layout or a native Agent Skill export."""
    if relative is None:
        relative = native_name
        if relative.startswith("skills/"):
            native_name = STAGE_BUNDLE_SPEC["native_name"]
        else:
            native_name = {
                "reproduction_skill/SKILL.md": "poc-reproduction",
                "submission_skill/SKILL.md": "poc-submission",
            }.get(relative, native_name)
    direct = root / relative
    if direct.is_file():
        return direct
    native = root / native_name / "SKILL.md"
    return native if native.is_file() else None


def _same_lessons(source_packet: Path, relative: str, source_text: str, installed_text: str) -> bool:
    """Compare every bullet, which is where the skill's instructions live.

    Native exports legitimately add YAML frontmatter and an adapter prelude, and
    every adapter substitutes different absolute paths, so neither the whole file
    nor a naive line diff is the right comparison. Bullets cover both the learned
    lessons and the fixed R.A / S.A loops, so an adapter cannot quietly edit
    either one. The current empty packet is
    ``skill_packets/dsh_deepseek_initial_two_layer_staged_100_100`` and has no lesson
    ids yet, so this must not depend on them.
    """
    expected = bullet_lines(source_text)
    source_dir = Path(relative).parent.as_posix()
    target = next(
        (name for name, section in targets_for_packet(source_packet).items()
         if Path(section.path).parent.as_posix() == source_dir),
        None,
    )
    targets = targets_for_packet(source_packet)
    if target is not None and (source_packet / targets[target].lesson_dir / "INDEX.md").is_file():
        expected.extend(
            f"- [{lesson['lesson_id']}] {lesson['text']}"
            for lesson in read_lessons(source_packet)[target]
        )
    return expected == bullet_lines(installed_text)


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


def _check_helpers(source_packet: Path, helpers_dir: Path) -> list[str]:
    # Both active staged layouts intentionally contain no helper skill.
    # Submission history is evaluator/harness metadata, not an agent-facing
    # skill dependency.  Keep the helper contract only for historical
    # two-layer packets so their old runs remain reproducible.
    if is_stage_packet(source_packet) or is_direct_stage_packet(source_packet):
        return []
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
