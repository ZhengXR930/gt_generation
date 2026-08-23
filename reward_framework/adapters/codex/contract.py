"""Codex adapter contract.

Codex already supports filesystem Agent Skills, so this adapter is intentionally
thin: export the two-layer packet into native SKILL.md folders and keep
benchmark submit/telemetry outside the learned lessons.
"""

from __future__ import annotations

import os
from pathlib import Path


ADAPTER_NAME = "codex"
INTERFACE_VERSION = "native-agent-skill-interface-v1"
SKILLS_DIR_ENV = "REWARD_FRAMEWORK_CODEX_SKILLS_DIR"


def default_skills_dir() -> Path:
    root = os.getenv("CODEX_HOME")
    if root:
        return Path(root).expanduser() / "skills"
    return Path.home() / ".codex" / "skills"


def resolve_skills_dir(value: str | None = None) -> Path:
    return Path(value or os.getenv(SKILLS_DIR_ENV) or default_skills_dir()).expanduser()
