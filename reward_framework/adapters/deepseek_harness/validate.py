#!/usr/bin/env python3
"""Validate DeepSeek Harness bundle export."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reward_framework.adapters.agent_skill_export import validate_native_agent_skills


def validate_bundle(bundle_dir: Path) -> dict:
    bundle_dir = bundle_dir.resolve()
    required = [
        "package.json",
        "cordis.patch.yml",
        "plugin/index.ts",
        "adapter_manifest.json",
        "skills/reward_framework_skill_export.json",
    ]
    for rel in required:
        path = bundle_dir / rel
        if not path.is_file():
            raise FileNotFoundError(f"missing DSH bundle file: {path}")
    pkg = json.loads((bundle_dir / "package.json").read_text())
    if "dsh" not in pkg or "bundle" not in pkg["dsh"]:
        raise ValueError("package.json missing dsh.bundle")
    patch_text = (bundle_dir / "cordis.patch.yml").read_text(encoding="utf-8")
    for needle in ["insert:", "id: reward-framework-poc-skills", "index.ts"]:
        if needle not in patch_text:
            raise ValueError(f"cordis.patch.yml missing expected insert syntax {needle!r}")
    plugin = (bundle_dir / "plugin/index.ts").read_text(encoding="utf-8")
    for needle in ["ctx.tools.register", "reward_framework_read_poc_skill"]:
        if needle not in plugin:
            raise ValueError(f"plugin missing expected DSH/Cordis hook {needle!r}")
    for needle in ["type: 'object'", "required: ['skill']", "additionalProperties: false"]:
        if needle not in plugin:
            raise ValueError(f"plugin tool schema missing expected raw JSON Schema text {needle!r}")
    skill_validation = validate_native_agent_skills(bundle_dir / "skills")
    return {"status": "pass", "bundle_dir": str(bundle_dir), "skills": skill_validation}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle-dir", required=True)
    args = ap.parse_args()
    print(json.dumps(validate_bundle(Path(args.bundle_dir)), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
