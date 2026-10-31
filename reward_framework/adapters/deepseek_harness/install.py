"""Export DeepSeek Harness native skill configuration for PoC skills."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reward_framework.adapters.agent_skill_export import export_native_agent_skills, inject_stage_skills_into_prompt

from reward_framework.adapters.deepseek_harness.contract import ADAPTER_NAME, INTERFACE_VERSION, resolve_bundle_dir
from reward_framework.adapters.base import SKILL_PACKET_ENV, substitute_skill_path_placeholders


def export_bundle(packet: Path, destination: Path | None = None) -> dict:
    dest = resolve_bundle_dir(str(destination) if destination else None).resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    skills_dir = dest / "skills"
    native_manifest = export_native_agent_skills(
        Path(packet),
        skills_dir,
        adapter_name=ADAPTER_NAME,
    )
    (dest / "package.json").write_text(
        json.dumps(
            {
                "name": "reward-framework-dsh-poc-skills",
                "version": "0.0.0",
                "private": True,
                "type": "module",
                "dsh": {"bundle": "cordis.patch.yml"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    write_native_skill_patch(dest / "cordis.patch.yml", skills_dir)
    (dest / "README.md").write_text(
        "# Reward Framework DeepSeek Harness Adapter\n\n"
        "This directory exports the current reward skill packet as native DSH "
        "filesystem skills. Mount `cordis.patch.yml` with `dsh --patch`; it "
        "configures the built-in `skill-filesystem` provider. Five-stage "
        "packets are exposed as one eagerly loaded lifecycle skill.\n",
        encoding="utf-8",
    )
    manifest = {
        "adapter": ADAPTER_NAME,
        "interface_version": INTERFACE_VERSION,
        "bundle_dir": str(dest),
        "native_skill_export": native_manifest,
        "patch_file": str(dest / "cordis.patch.yml"),
        "native_skill_root": str(skills_dir.resolve()),
        "dsh_skill_provider_config": {
            "includeDefaultRoots": False,
            "customSkillDirs": [str(skills_dir.resolve())],
            "watch": False,
        },
    }
    (dest / "adapter_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def write_native_skill_patch(path: Path, skills_dir: Path) -> None:
    path.write_text(
        "- id: skill-filesystem\n"
        "  config:\n"
        "    includeDefaultRoots: false\n"
        "    customSkillDirs:\n"
        f"      - '{str(skills_dir.resolve()).replace(chr(39), chr(39) + chr(39))}'\n"
        "    watch: false\n",
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
    inject_stage_skills_into_prompt(packet, env, scratch)
    skills_dir = workspace / ".dsh" / "skills"
    native_manifest = export_native_agent_skills(
        packet,
        skills_dir,
        adapter_name=ADAPTER_NAME,
    )
    metadata_file = skills_dir / "reward_framework_skill_export.json"
    if metadata_file.exists():
        metadata_file.unlink()
    manifest = {
        "adapter": ADAPTER_NAME,
        "interface_version": INTERFACE_VERSION,
        "install_mode": "workspace-dsh-skills",
        "native_skill_export": native_manifest,
        "native_skill_root": str(skills_dir.resolve()),
    }
    # Legacy packets may expose submission helpers.  The five-stage packet is
    # deliberately helper-free; its five source stages are compiled into one
    # agent-facing lifecycle skill and no helper path is injected.
    agent_workspace = Path("/workspace")
    agent_skills_dir = agent_workspace / ".dsh" / "skills"
    state_dir = skills_dir.parent / "state"
    state_dir.mkdir(exist_ok=True)
    patch_file = scratch / "dsh_native_skills.patch.yml"
    write_native_skill_patch(patch_file, agent_skills_dir)
    env["HARNESS_DSH_PATCH_FILE"] = str(patch_file)
    helper_skill = native_manifest.get("helper_skill")
    helper_root = agent_skills_dir / str(helper_skill) / "helpers" if helper_skill else None
    if helper_root is not None:
        substitute_skill_path_placeholders(
            skills_dir,
            helpers_dir=helper_root,
            state_dir=agent_workspace / ".dsh" / "state",
            workspace=agent_workspace,
        )
    manifest["agent_paths"] = {
        "skill_packet": str(skills_dir),
        "helpers": str(helper_root or (agent_skills_dir / "helpers")),
        "state": str(state_dir),
        "workspace": str(workspace),
        "dsh_patch_file": str(patch_file),
    }
    manifest["agent_paths"] = {
        "skill_packet": str(agent_skills_dir),
        "helpers": str(helper_root or (agent_skills_dir / "helpers")),
        "state": str(agent_workspace / ".dsh" / "state"),
        "workspace": str(agent_workspace),
        "dsh_patch_file": "/run/gt-dsh-runtime/patch.yml",
    }
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packet", required=True)
    ap.add_argument("--destination")
    args = ap.parse_args()
    result = export_bundle(
        Path(args.packet),
        Path(args.destination) if args.destination else None,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
