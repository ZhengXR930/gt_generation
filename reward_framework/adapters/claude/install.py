"""Install reward-framework PoC skills for Claude Code / Agent Skills."""

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

from reward_framework.adapters.claude.contract import ADAPTER_NAME, INTERFACE_VERSION, resolve_skills_dir
from reward_framework.adapters.base import SKILL_PACKET_ENV


def install_skill_packet(
    packet: Path,
    destination: Path | None = None,
    *,
    project_dir: Path | None = None,
    write_claude_md: bool = False,
) -> dict:
    dest = resolve_skills_dir(str(destination) if destination else None)
    manifest = export_native_agent_skills(packet, dest, adapter_name=ADAPTER_NAME)
    manifest["interface_version"] = INTERFACE_VERSION
    if project_dir:
        project_dir = Path(project_dir)
        bridge = project_dir / ".claude" / "reward_framework_poc_skills.md"
        write_bridge_file(bridge, adapter_name=ADAPTER_NAME, skills_dir=dest)
        manifest["project_bridge"] = str(bridge)
        if write_claude_md:
            claude_md = project_dir / "CLAUDE.md"
            marker = "<!-- reward-framework-poc-skills -->"
            block = (
                f"\n{marker}\n"
                "Use the installed `poc-reproduction` and "
                "`poc-submission` skills for benchmark PoC "
                "reproduction tasks. Details: `.claude/reward_framework_poc_skills.md`.\n"
                f"{marker.replace('<!--', '<!-- /')}\n"
            )
            existing = claude_md.read_text(encoding="utf-8", errors="replace") if claude_md.exists() else ""
            if marker not in existing:
                claude_md.write_text(existing.rstrip() + block, encoding="utf-8")
            manifest["claude_md"] = str(claude_md)
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
    config_dir = scratch / "claude_config"
    skills_dir = config_dir / "skills"
    manifest = install_skill_packet(packet, skills_dir, project_dir=workspace)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    manifest["workspace"] = str(workspace)
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packet", required=True)
    ap.add_argument("--destination")
    ap.add_argument("--project-dir")
    ap.add_argument("--write-claude-md", action="store_true")
    args = ap.parse_args()
    result = install_skill_packet(
        Path(args.packet),
        Path(args.destination) if args.destination else None,
        project_dir=Path(args.project_dir) if args.project_dir else None,
        write_claude_md=args.write_claude_md,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
