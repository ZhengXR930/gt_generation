"""Install the OpenHands skill adapter into a generated benchmark workspace."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from reward_framework.adapters.base import substitute_skill_path_placeholders

from .contract import (
    SKILL_PACKET_ENV,
    WORKSPACE_HELPERS_DIR,
    WORKSPACE_SKILL_PACKET_DIR,
    WORKSPACE_STATE_DIR,
    packet_metadata,
)


def install_workspace_skill_packet(
    *,
    harness: str,
    workspace: Path,
    sample_id: str,
    scratch: Path,
    env: dict[str, str],
) -> dict | None:
    """Copy the configured frozen skill packet into an OpenHands workspace.

    The neutral runtime calls this only when the reward adapter explicitly
    passes it as HARNESS_WORKSPACE_INSTALLER.
    """
    del harness, scratch
    source_raw = str(env.get(SKILL_PACKET_ENV) or os.getenv(SKILL_PACKET_ENV, "")).strip()
    if not source_raw:
        return None
    source = Path(source_raw).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"{SKILL_PACKET_ENV} does not exist: {source}")

    packet_dst = workspace / WORKSPACE_SKILL_PACKET_DIR
    if packet_dst.exists():
        shutil.rmtree(packet_dst)
    shutil.copytree(
        source,
        packet_dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    helpers_dst = workspace / WORKSPACE_HELPERS_DIR
    if helpers_dst.exists():
        shutil.rmtree(helpers_dst)
    helpers_dst.mkdir()
    copied_helpers: list[str] = []
    for helper in sorted(packet_dst.glob("*/helpers/*.py")):
        target = helpers_dst / helper.name
        shutil.copy2(helper, target)
        copied_helpers.append(str(target.relative_to(workspace)))

    state_dir = workspace / WORKSPACE_STATE_DIR
    state_dir.mkdir(exist_ok=True)
    substitute_skill_path_placeholders(
        packet_dst,
        helpers_dir=helpers_dst,
        state_dir=state_dir,
        workspace=workspace,
    )
    return packet_metadata(
        source,
        copied_helpers,
        False,
        sample_id,
        workspace=workspace,
    )
