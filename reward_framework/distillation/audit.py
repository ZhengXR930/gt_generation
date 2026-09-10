"""Mechanical self-check of the distillation framework.

Layout checks catch a packet that lost a file. The isolation checks matter more:
they exercise the real lint and the real update path to prove that ground-truth
text and evidence cannot reach a skill packet, rather than trusting that the
prompts asked nicely.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

from reward_framework.adapters.base import DEFAULT_SKILL_PACKET

from .defaults import INITIAL_PACKET, REPO_ROOT
from .lint import lint_lesson_text, load_literal_index
from .skill_packet import TARGETS, apply_curator_decisions, copy_initial_packet

TEMPLATES = REPO_ROOT / "reward_framework" / "distillation" / "prompt_templates"
REQUIRED_TEMPLATES = ("README.md", "diagnostician.md", "teacher.md", "curator.md", "correction.md")
REQUIRED_HELPERS = ["submit_history.py", "submit_preflight.py"]

# Wording from earlier designs that must not creep back into the agent's task.
FORBIDDEN_PROMPT_TEXT = (
    "Do not finish this generation task before at least one submit.sh attempt",
)

_GENERIC_LESSON = "Preserve container framing while changing the smallest field that reaches the vulnerable branch."
_ANCHORED_LESSON = "Set the length at parser.c:120 to 0xdeadbeef."
_SENTINELS = ("SENTINEL_EVIDENCE_MUST_NOT_SHIP", "SENTINEL_RATIONALE_MUST_NOT_SHIP")


def audit_framework() -> dict[str, Any]:
    errors = [*_layout_errors(), *_contract_errors(), *_isolation_errors()]
    return {"status": "fail" if errors else "pass", "errors": errors}


def _layout_errors() -> Iterator[str]:
    """The files the framework cannot run without."""
    if not (REPO_ROOT / "reward_framework" / "coding_agent_boundary.md").is_file():
        yield "missing coding_agent_boundary.md"
    if (TEMPLATES / "coding_agent.md").exists():
        yield "coding_agent.md is back under prompt_templates; the coding agent is not a distillation role"
    if not (REPO_ROOT / "reward_framework" / "skill_distillation_plan.md").is_file():
        yield "missing skill_distillation_plan.md"
    if (REPO_ROOT / "reward_framework" / "offline_static_distillation").exists():
        yield "offline_static_distillation still exists"

    required = [
        INITIAL_PACKET / "reproduction_skill" / "SKILL.md",
        INITIAL_PACKET / "submission_skill" / "SKILL.md",
        *(INITIAL_PACKET / "submission_skill" / "helpers" / name for name in REQUIRED_HELPERS),
        *(TEMPLATES / name for name in REQUIRED_TEMPLATES),
    ]
    yield from (f"missing {path}" for path in required if not path.is_file())


def _contract_errors() -> Iterator[str]:
    """Rules the design depends on: helper scope, fixed sections, prompt purity."""
    repro_helpers = INITIAL_PACKET / "reproduction_skill" / "helpers"
    if repro_helpers.is_dir() and list(repro_helpers.glob("*.py")):
        yield "reproduction skill contains helper scripts"

    helpers = sorted(path.name for path in (INITIAL_PACKET / "submission_skill" / "helpers").glob("*.py"))
    if helpers != REQUIRED_HELPERS:
        yield f"unexpected submission helpers: {helpers}"

    yield from (f"fixed section {name} is writable" for name in ("reproduction:R.A", "submission:S.A") if name in TARGETS)

    prompt = _text(REPO_ROOT / "reward_framework" / "prompt.txt")
    yield from (f"forbidden prompt text remains: {text}" for text in FORBIDDEN_PROMPT_TEXT if text in prompt)

    if DEFAULT_SKILL_PACKET != INITIAL_PACKET:
        yield f"DEFAULT_SKILL_PACKET mismatch: {DEFAULT_SKILL_PACKET} != {INITIAL_PACKET}"
    if "fine_trace_coverage" not in _text(REPO_ROOT / "evaluator" / "evaluate.py"):
        yield "evaluator does not include fine_trace_coverage"


def _isolation_errors() -> list[str]:
    """Run the real lint and the real update path against known-bad text."""
    index = load_literal_index()
    errors: list[str] = []

    gt_function = _any_ground_truth_function()
    if gt_function is None:
        return ["no ground_truth.json available to audit packet isolation"]
    if not lint_lesson_text(f"Remember that {gt_function} is where the check is missing.", index):
        errors.append("packet lint does not reject a ground-truth function name")
    if not lint_lesson_text(_ANCHORED_LESSON, index):
        errors.append("packet lint does not reject file:line and hex anchors")
    if lint_lesson_text(_GENERIC_LESSON, index):
        errors.append("packet lint rejects a generic transferable lesson")

    errors += _evidence_leak_errors(index)
    return errors


def _evidence_leak_errors(index: dict[str, Any]) -> list[str]:
    """Apply a decision carrying sentinel evidence and confirm none of it ships."""
    workdir = Path(tempfile.mkdtemp(prefix="reward-audit-"))
    try:
        result = apply_curator_decisions(
            copy_initial_packet(workdir / "src"),
            [{
                "decision": "MODIFY",
                "target": "submission:S.C",
                "proposal": "Distinguish parser failure from sink miss when reading a non-triggering result.",
                "evidence": _SENTINELS[0],
                "rationale": _SENTINELS[1],
            }],
            out_packet=workdir / "out",
            literal_index=index,
        )
        if not result["applied"]:
            return [f"audit decision was not applied: {result['skipped']}"]
        shipped = "\n".join(
            _text(path) for path in (workdir / "out").rglob("*")
            if path.is_file() and path.suffix in {".md", ".json", ".txt"}
        )
        return [f"{sentinel} reached the packet" for sentinel in _SENTINELS if sentinel in shipped]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _any_ground_truth_function() -> str | None:
    for sample in sorted((REPO_ROOT / "gt_results").iterdir()):
        path = sample / "ground_truth.json"
        if not path.is_file():
            continue
        steps = [item for item in (json.loads(path.read_text(encoding="utf-8")).get("fine_trace") or []) if isinstance(item, dict)]
        if steps and steps[0].get("function"):
            return str(steps[0]["function"])
    return None


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")
