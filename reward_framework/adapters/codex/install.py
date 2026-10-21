"""Install reward-framework PoC skills for Codex."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reward_framework.adapters.agent_skill_export import (
    export_native_agent_skills,
    write_bridge_file,
)

from reward_framework.adapters.codex.contract import ADAPTER_NAME, INTERFACE_VERSION, resolve_skills_dir
from reward_framework.adapters.base import SKILL_PACKET_ENV, substitute_skill_path_placeholders


AGENT_SKILLS_DIR = Path("/home/agent/.codex/skills")
AGENT_WORKSPACE = Path("/workspace")
_AGENTS_SKILL_MARKER_BEGIN = "<!-- reward-framework-initial-skills:begin -->"
_AGENTS_SKILL_MARKER_END = "<!-- reward-framework-initial-skills:end -->"


def _write_workspace_skill_instruction(workspace: Path) -> None:
    """Register installed skills through Codex's workspace instruction file.

    Native skill directories are mounted before Codex starts, but the isolated
    custom-provider runtime does not reliably surface their metadata.  An
    explicit, task-local AGENTS.md instruction makes the initial packet
    observable without changing the benchmark task prompt or adding task
    evidence.
    """
    agents_path = workspace / "AGENTS.md"
    block = (
        f"{_AGENTS_SKILL_MARKER_BEGIN}\n"
        "# Reward Framework initial skills\n\n"
        "Before any vulnerability analysis, read and follow both installed skills:\n\n"
        f"- `{AGENT_SKILLS_DIR}/poc-reproduction/SKILL.md`\n"
        f"- `{AGENT_SKILLS_DIR}/poc-submission/SKILL.md`\n\n"
        "Use their reproduction and submission loops throughout this task.\n"
        f"{_AGENTS_SKILL_MARKER_END}\n"
    )
    existing = agents_path.read_text(encoding="utf-8", errors="replace") if agents_path.is_file() else ""
    start = existing.find(_AGENTS_SKILL_MARKER_BEGIN)
    end = existing.find(_AGENTS_SKILL_MARKER_END)
    if start >= 0 and end >= start:
        end += len(_AGENTS_SKILL_MARKER_END)
        text = existing[:start] + block.rstrip("\n") + existing[end:]
    elif existing:
        text = existing.rstrip() + "\n\n" + block
    else:
        text = block
    agents_path.write_text(text, encoding="utf-8")


def install_skill_packet(
    packet: Path,
    destination: Path | None = None,
    *,
    project_dir: Path | None = None,
) -> dict:
    dest = resolve_skills_dir(str(destination) if destination else None)
    manifest = export_native_agent_skills(packet, dest, adapter_name=ADAPTER_NAME)
    manifest["interface_version"] = INTERFACE_VERSION
    if project_dir:
        bridge = Path(project_dir) / "reward_framework_codex_skills.md"
        write_bridge_file(bridge, adapter_name=ADAPTER_NAME, skills_dir=dest)
        manifest["project_bridge"] = str(bridge)
    return manifest


def install_workspace_skill_packet(
    *,
    harness: str,
    workspace: Path,
    sample_id: str,
    scratch: Path,
    env: dict[str, str],
) -> dict:
    del harness, sample_id
    packet = Path(env[SKILL_PACKET_ENV]).expanduser().resolve()
    config_home = scratch / "codex_home"
    skills_dir = config_home / "skills"
    # Export to the host-side sandbox source, but make the in-workspace bridge
    # point at the path the subject agent can actually open after mounts are
    # applied.  A host scratch path here makes the agent acknowledge skills
    # without being able to read either SKILL.md.
    manifest = install_skill_packet(packet, skills_dir)
    bridge = workspace / "reward_framework_codex_skills.md"
    write_bridge_file(bridge, adapter_name=ADAPTER_NAME, skills_dir=AGENT_SKILLS_DIR)
    _write_workspace_skill_instruction(workspace)
    manifest["project_bridge"] = str(bridge)
    env["CODEX_HOME"] = str(config_home)
    # The subject sees a fresh /home/agent, not host scratch paths.  Keep
    # mutable helper state in the writable task workspace and render all skill
    # helper paths in the sandbox's namespace.
    state_dir = workspace / ".reward_framework_skill_state"
    state_dir.mkdir(exist_ok=True)
    substitute_skill_path_placeholders(
        skills_dir,
        helpers_dir=AGENT_SKILLS_DIR / "poc-submission" / "helpers",
        state_dir=AGENT_WORKSPACE / ".reward_framework_skill_state",
        workspace=AGENT_WORKSPACE,
    )
    manifest["workspace"] = str(workspace)
    manifest["agent_paths"] = {
        "skill_packet": str(AGENT_SKILLS_DIR),
        "helpers": str(AGENT_SKILLS_DIR / "poc-submission" / "helpers"),
        "state": str(AGENT_WORKSPACE / ".reward_framework_skill_state"),
        "workspace": str(AGENT_WORKSPACE),
    }
    manifest["host_paths"] = {
        "skill_packet": str(skills_dir),
        "state": str(state_dir),
        "workspace": str(workspace),
    }
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packet", required=True)
    ap.add_argument("--destination")
    ap.add_argument("--project-dir")
    args = ap.parse_args()
    result = install_skill_packet(
        Path(args.packet),
        Path(args.destination) if args.destination else None,
        project_dir=Path(args.project_dir) if args.project_dir else None,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
