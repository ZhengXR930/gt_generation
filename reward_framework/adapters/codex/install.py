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
)

from reward_framework.adapters.codex.contract import ADAPTER_NAME, INTERFACE_VERSION, resolve_skills_dir
from reward_framework.adapters.base import SKILL_PACKET_ENV, substitute_skill_path_placeholders


WORKSPACE_SKILL_DIRNAME = ".reward_framework_skills"
WORKSPACE_STATE_DIRNAME = ".reward_framework_state"
WORKSPACE_BRIDGE_NAME = "REWARD_FRAMEWORK_SKILLS.md"


def install_skill_packet(
    packet: Path,
    destination: Path | None = None,
    *,
    project_dir: Path | None = None,
) -> dict:
    del project_dir
    dest = resolve_skills_dir(str(destination) if destination else None)
    manifest = export_native_agent_skills(packet, dest, adapter_name=ADAPTER_NAME)
    manifest["interface_version"] = INTERFACE_VERSION
    return manifest


def _write_workspace_bridge(path: Path, *, skills_dir: Path) -> None:
    reproduction = skills_dir / "poc-reproduction" / "SKILL.md"
    submission = skills_dir / "poc-submission" / "SKILL.md"
    path.write_text(
        "# Reward Framework PoC Skills\n\n"
        "Read these workspace-readable skill files before the first candidate; "
        "they mirror the native Codex skills without exposing CODEX_HOME:\n\n"
        f"- `{reproduction.relative_to(path.parent)}`\n"
        f"- `{submission.relative_to(path.parent)}`\n",
        encoding="utf-8",
    )


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
    native_skills_dir = config_home / "skills"
    manifest = install_skill_packet(packet, native_skills_dir, project_dir=workspace)
    env["CODEX_HOME"] = str(config_home)

    native_state_dir = scratch / "state"
    native_state_dir.mkdir(exist_ok=True)
    substitute_skill_path_placeholders(
        native_skills_dir,
        helpers_dir=native_skills_dir / "poc-submission" / "helpers",
        state_dir=native_state_dir,
        workspace=workspace,
    )

    workspace_skills_dir = workspace / WORKSPACE_SKILL_DIRNAME
    workspace_manifest = export_native_agent_skills(
        packet,
        workspace_skills_dir,
        adapter_name=ADAPTER_NAME,
    )
    workspace_state_dir = workspace / WORKSPACE_STATE_DIRNAME
    workspace_state_dir.mkdir(exist_ok=True)
    substitute_skill_path_placeholders(
        workspace_skills_dir,
        helpers_dir=workspace_skills_dir / "poc-submission" / "helpers",
        state_dir=workspace_state_dir,
        workspace=workspace,
    )
    bridge_file = workspace / WORKSPACE_BRIDGE_NAME
    _write_workspace_bridge(bridge_file, skills_dir=workspace_skills_dir)

    manifest["workspace"] = str(workspace)
    manifest["native_skill_root"] = str(native_skills_dir)
    manifest["workspace_skill_root"] = str(workspace_skills_dir)
    manifest["workspace_bridge"] = str(bridge_file)
    manifest["workspace_skill_export"] = workspace_manifest
    manifest["agent_paths"] = {
        "skill_packet": str(workspace_skills_dir),
        "native_skill_packet": str(native_skills_dir),
        "helpers": str(workspace_skills_dir / "poc-submission" / "helpers"),
        "state": str(workspace_state_dir),
        "workspace": str(workspace),
        "workspace_bridge": str(bridge_file),
    }
    env["REWARD_FRAMEWORK_WORKSPACE_SKILLS_DIR"] = str(workspace_skills_dir)
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
