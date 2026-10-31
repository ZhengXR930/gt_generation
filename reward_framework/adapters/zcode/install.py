"""Install reward-framework skills through ZCode's native skill interface."""

from __future__ import annotations

from pathlib import Path

from reward_framework.adapters.agent_skill_export import export_native_agent_skills
from reward_framework.adapters.base import (
    SKILL_PACKET_ENV,
    substitute_skill_path_placeholders,
)


ADAPTER_NAME = "zcode"
INTERFACE_VERSION = "zcode-native-skill-interface-v3"
AGENT_SKILLS_DIR = Path("/workspace/.zcode/skills")
# This is the only runtime trigger for native ZCode skill loading.  Keep the
# reward-framework adapter and the ZCode CLI on the same public environment
# variable; the old REWARD_FRAMEWORK_ZCODE_* skill-name variables were an
# adapter-internal second trigger and made a fresh session easy to miswire.
ZCODE_PRELOAD_SKILLS_ENV = "ZCODE_PRELOAD_SKILLS"


def install_workspace_skill_packet(
    *,
    harness: str,
    workspace: Path,
    sample_id: str,
    scratch: Path,
    env: dict[str, str],
) -> dict:
    """Install the packet in the project skill root that ZCode scans.

    ZCode discovers native skills from ``<cwd>/.zcode/skills`` (and exposes
    them through its Skill tool).  The generated benchmark workspace is the
    project cwd, so keeping the packet there makes discovery, loading, and
    skill telemetry use the same path.  Do not inject the packet into the task
    prompt: that would make a ZCode run look skill-enabled while bypassing the
    harness's actual skill mechanism.
    """
    del harness, sample_id, scratch
    packet = Path(env[SKILL_PACKET_ENV]).expanduser().resolve()
    destination = workspace / ".zcode" / "skills"
    manifest = export_native_agent_skills(
        packet,
        destination,
        adapter_name=ADAPTER_NAME,
    )

    # Legacy two-layer packets may refer to helper/state paths symbolically.
    # Rewrite only the isolated copy; the source packet remains immutable for
    # Teacher/Curator and for packet hashing.
    state_dir = workspace / ".reward_framework_skill_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    helper_skill = manifest.get("helper_skill")
    if helper_skill:
        substitute_skill_path_placeholders(
            destination,
            helpers_dir=destination / str(helper_skill) / "helpers",
            state_dir=Path("/workspace/.reward_framework_skill_state"),
            workspace=Path("/workspace"),
        )

    manifest.update(
        {
            "interface_version": INTERFACE_VERSION,
            "workspace": str(workspace),
            "prompt_injected": None,
            # The harness asks the ZCode runtime to preload every exported
            # native skill before the first task turn.  The skill bodies stay
            # outside the ordinary task prompt and are represented in the
            # provider history as native Skill call/result pairs.
            "skill_load": {
                "mode": "native-preloaded",
                "trigger_env": ZCODE_PRELOAD_SKILLS_ENV,
                "initial_skill": None,
                "available_skills": [
                    str(item["name"])
                    for item in manifest.get("skills", [])
                    if item.get("name")
                ],
                "required_skills": [
                    str(item["name"])
                    for item in manifest.get("skills", [])
                    if item.get("name")
                ],
                "preload_skills": [
                    str(item["name"])
                    for item in manifest.get("skills", [])
                    if item.get("name")
                ],
            },
            "skill_root": str(AGENT_SKILLS_DIR),
            "agent_paths": {
                "skill_packet": str(AGENT_SKILLS_DIR),
                "helpers": (
                    str(AGENT_SKILLS_DIR / str(helper_skill) / "helpers")
                    if helper_skill
                    else str(AGENT_SKILLS_DIR / "helpers")
                ),
                "state": "/workspace/.reward_framework_skill_state",
                "workspace": "/workspace",
            },
            "host_paths": {
                "skill_packet": str(destination),
                "state": str(state_dir),
                "workspace": str(workspace),
            },
        }
    )
    exported_names = [
        str(item["name"])
        for item in manifest.get("skills", [])
        if item.get("name")
    ]
    if exported_names:
        # The adapter writes the canonical trigger directly.  Every new ZCode
        # session receives this value through the isolated environment and
        # preloads the same complete skill set before the first prompt.
        env[ZCODE_PRELOAD_SKILLS_ENV] = ",".join(dict.fromkeys(exported_names))
    else:
        env.pop(ZCODE_PRELOAD_SKILLS_ENV, None)
    # Remove the transitional aliases if they were inherited from an older
    # shell/session.  They must never influence skill loading again.
    env.pop("REWARD_FRAMEWORK_ZCODE_SKILL_NAMES", None)
    env.pop("REWARD_FRAMEWORK_ZCODE_PRELOAD_SKILLS", None)
    return manifest
