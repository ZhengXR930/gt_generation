"""Claude adapter contract.

Claude Code reads skills and project memory from `.claude`/`~/.claude`.
This adapter exports the two-layer packet as native SKILL.md folders and can
optionally write a project bridge file.
"""

from __future__ import annotations

import os
from pathlib import Path


ADAPTER_NAME = "claude"
INTERFACE_VERSION = "native-agent-skill-interface-v1"
SKILLS_DIR_ENV = "REWARD_FRAMEWORK_CLAUDE_SKILLS_DIR"


def default_config_dir() -> Path:
    return Path(os.getenv("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()


def default_skills_dir() -> Path:
    return default_config_dir() / "skills"


def resolve_skills_dir(value: str | None = None) -> Path:
    return Path(value or os.getenv(SKILLS_DIR_ENV) or default_skills_dir()).expanduser()
