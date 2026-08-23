"""Export a DeepSeek Harness bundle/plugin scaffold for PoC skills."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reward_framework.adapters.agent_skill_export import export_native_agent_skills

from reward_framework.adapters.deepseek_harness.contract import ADAPTER_NAME, INTERFACE_VERSION, resolve_bundle_dir


PLUGIN_TS = """import { readFileSync } from 'node:fs'
import { join } from 'node:path'

export const name = 'reward-framework-poc-skills'
export const inject = ['tools']

const root = new URL('..', import.meta.url).pathname

function readSkill(name) {
  return readFileSync(join(root, 'skills', name, 'SKILL.md'), 'utf8')
}

export function apply(ctx) {
  ctx.tools.register({
    name: 'reward_framework_read_poc_skill',
    description: 'Read a reward-framework PoC reproduction skill by name.',
    parameters: {
      type: 'object',
      properties: {
        skill: {
          type: 'string',
          enum: ['poc-submission-verification', 'poc-vulnerability-reproduction'],
          description: 'Skill to read.',
        },
      },
      required: ['skill'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      return readSkill(args.skill)
    },
  })

  ctx.on('tools/result', (exec, result) => {
    if (String(exec.name).startsWith('reward_framework_')) return
    const text = result.content.map(block => block.type === 'text' ? block.text : '').join('')
    if (text.includes('submit') || text.includes('analysis.json')) {
      console.log(`[reward-framework] observed tool result from ${exec.name}`)
    }
  })
}
"""

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
    plugin_dir = dest / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "index.ts").write_text(PLUGIN_TS, encoding="utf-8")
    (dest / "package.json").write_text(
        json.dumps(
            {
                "name": "reward-framework-dsh-poc-skills",
                "version": "0.0.0",
                "private": True,
                "type": "module",
                "dsh": {"bundle": "cordis.patch.yml"},
                "dependencies": {
                    "@deepseek-ai/cordis": "*",
                    "@deepseek-ai/dsh-tools": "*",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    plugin_entry = (plugin_dir / "index.ts").resolve()
    (dest / "cordis.patch.yml").write_text(
        "- insert:\n"
        "    - id: reward-framework-poc-skills\n"
        f"      name: '{plugin_entry}'\n",
        encoding="utf-8",
    )
    (dest / "README.md").write_text(
        "# Reward Framework DeepSeek Harness Adapter\n\n"
        "This directory is a DeepSeek Harness bundle scaffold. Mount it as a "
        "Cordis/DSH bundle or patch overlay according to the local DSH profile. "
        "It does not patch DeepSeek Harness core.\n\n"
        "The plugin exposes a model-callable skill reader tool and observes tool "
        "results for telemetry hooks. Benchmark submit tools should be supplied "
        "by the surrounding evaluation harness.\n",
        encoding="utf-8",
    )
    manifest = {
        "adapter": ADAPTER_NAME,
        "interface_version": INTERFACE_VERSION,
        "bundle_dir": str(dest),
        "native_skill_export": native_manifest,
        "patch_file": str(dest / "cordis.patch.yml"),
        "plugin_entry": str(plugin_dir / "index.ts"),
    }
    (dest / "adapter_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
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
