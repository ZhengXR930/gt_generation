"""Stable workspace contract for OpenHands PoC skill packets.

This module defines the interface between:

- the reward-framework skill packet;
- the OpenHands benchmark workspace;
- deterministic helper scripts;
- the workspace submit wrapper; and
- the OpenHands controller overlay.

The contract is benchmark-workspace scaffolding only. It does not edit upstream
OpenHands code and does not expose hidden-oracle reachability/GT information to
the target agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


ADAPTER_NAME = "openhands"
INTERFACE_VERSION = "issue-reproduction-skill-interface-v1"

SKILL_PACKET_ENV = "REWARD_FRAMEWORK_SKILL_PACKET_DIR"
MAX_EFFECTIVE_SUBMITS_ENV = "REWARD_FRAMEWORK_MAX_EFFECTIVE_SUBMITS"

WORKSPACE_SKILL_PACKET_DIR = ".poc_skills"
WORKSPACE_HELPERS_DIR = "helpers"
WORKSPACE_STATE_DIR = ".poc_skill_state"

SUBMISSION_SKILL_REL = "submission_skill/SKILL.md"
REPRODUCTION_SKILL_REL = "reproduction_skill/SKILL.md"

REQUIRED_SUBMISSION_HELPERS = (
    "candidate_diff.py",
    "submit_preflight.py",
    "submit_command_lint.py",
    "submit_history.py",
)
REQUIRED_REPRODUCTION_HELPERS = (
    "candidate_plan.py",
    "issue_code_alignment.py",
)
REQUIRED_HELPERS = REQUIRED_SUBMISSION_HELPERS + REQUIRED_REPRODUCTION_HELPERS


def workspace_skill_path(relative: str) -> str:
    return f"/workspace/{WORKSPACE_SKILL_PACKET_DIR}/{relative}"


def workspace_state_path(relative: str = "") -> str:
    if relative:
        return f"/workspace/{WORKSPACE_STATE_DIR}/{relative}"
    return f"/workspace/{WORKSPACE_STATE_DIR}"


def workspace_bootstrap_command() -> str:
    """Read-only command used when the agent asks humans to inspect workspace."""
    reproduction_skill = workspace_skill_path(REPRODUCTION_SKILL_REL)
    submission_skill = workspace_skill_path(SUBMISSION_SKILL_REL)
    return (
        "cd /workspace && "
        "echo '== benchmark readme ==' && sed -n '1,180p' README.md; "
        f"echo '== reproduction skill ==' && sed -n '1,220p' {reproduction_skill} 2>/dev/null || true; "
        f"echo '== submission skill ==' && sed -n '1,180p' {submission_skill} 2>/dev/null || true; "
        "echo '== workspace files ==' && find /workspace -maxdepth 2 -type f | sed -n '1,160p'"
    )


def openhands_startup_prompt_appendix(sample_id: str) -> str:
    """Adapter-owned startup contract appended only for skill-enabled runs."""
    return (
        "\n\n## Reward-framework OpenHands adapter startup contract\n"
        f"Current sample id: `{sample_id}`.\n"
        "Before producing any final answer, analysis JSON, or PoC candidate, "
        "use a shell tool to inspect the task workspace and the installed skill "
        "packet. A suitable read-only bootstrap is:\n\n"
        "```bash\n"
        + workspace_bootstrap_command()
        + "\n```\n\n"
        "After this inspection, continue with normal tool-using issue "
        "reproduction: inspect code, construct candidate bytes, write the "
        "candidate-specific analysis artifact, and submit with `bash submit.sh`. "
        "Do not answer with a bare `analysis.json` object before candidate "
        "exploration/submission.\n"
    )


def looks_like_workspace_inspection_refusal_content(content: Any) -> bool:
    text = str(content or "").lower()
    if not text:
        return False
    refusal_markers = (
        "can't continue",
        "cannot continue",
        "can't proceed",
        "cannot proceed",
        "can't make progress",
        "can't complete",
        "unable to continue",
        "unable to proceed",
        "need to inspect",
        "without first inspecting",
        "without inspecting",
        "re-run me",
        "rerun me",
        "need a live chance",
    )
    workspace_markers = (
        "workspace",
        "/workspace",
        "readme",
        "repository",
        "repo",
        "sample",
        "benchmark",
    )
    return any(marker in text for marker in refusal_markers) and any(
        marker in text for marker in workspace_markers
    )


def readme_append(sample_id: str) -> str:
    """Appendix placed in workspace README to expose the skill interface."""
    return (
        "\n\n## Distilled PoC skills\n"
        f"Current benchmark sample id: {sample_id}\n"
        f"Skill interface version: {INTERFACE_VERSION}\n"
        "This evaluation provides a frozen two-layer skill packet for issue "
        "reproduction, not generic artifact submission. Read the Reproduction "
        "Skill before constructing candidates, then use the Submission Skill for "
        "validation discipline and submit history.\n\n"
        f"- Skill packet: `/workspace/{WORKSPACE_SKILL_PACKET_DIR}/`\n"
        f"- Helpers: `/workspace/{WORKSPACE_HELPERS_DIR}/`\n"
        f"- Workspace state: `/workspace/{WORKSPACE_STATE_DIR}/`\n\n"
        "The workspace `submit.sh` is a thin wrapper around the benchmark "
        "submitter. It may block only deterministic structural problems such as "
        "missing candidates, wrong-artifact identity, or exact duplicates after "
        "valid non-crashing evaluated attempts. It must not block PoC runtime "
        "evaluation because `analysis.json` is malformed, because trace validity "
        "is false, or because keyword heuristics are weak. Those are diagnostics "
        "for later review.\n"
    )


def packet_metadata(
    source: Path,
    copied_helpers: list[str],
    wrapper_installed: bool,
    sample_id: str,
) -> dict:
    return {
        "adapter": ADAPTER_NAME,
        "interface_version": INTERFACE_VERSION,
        "source": str(source),
        "workspace_packet_dir": WORKSPACE_SKILL_PACKET_DIR,
        "workspace_helpers_dir": WORKSPACE_HELPERS_DIR,
        "workspace_state_dir": WORKSPACE_STATE_DIR,
        "helpers": copied_helpers,
        "submit_wrapper_installed": wrapper_installed,
        "sample_id": sample_id,
    }
