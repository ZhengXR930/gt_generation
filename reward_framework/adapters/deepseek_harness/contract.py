"""DeepSeek Harness adapter contract."""

from __future__ import annotations

import os
from pathlib import Path


ADAPTER_NAME = "deepseek_harness"
INTERFACE_VERSION = "dsh-plugin-bundle-interface-v1"
BUNDLE_DIR_ENV = "REWARD_FRAMEWORK_DSH_BUNDLE_DIR"


def resolve_bundle_dir(value: str | None = None) -> Path:
    return Path(value or os.getenv(BUNDLE_DIR_ENV) or "reward-framework-dsh-bundle").expanduser()
