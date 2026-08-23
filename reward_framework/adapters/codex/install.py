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
