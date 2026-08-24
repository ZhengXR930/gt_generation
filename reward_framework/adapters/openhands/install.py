"""Install the OpenHands skill adapter into a generated benchmark workspace."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .contract import (
    MAX_EFFECTIVE_SUBMITS_ENV,
    SKILL_PACKET_ENV,
    WORKSPACE_HELPERS_DIR,
    WORKSPACE_SKILL_PACKET_DIR,
    WORKSPACE_STATE_DIR,
    packet_metadata,
    readme_append,
)


def _submit_wrapper_text() -> str:
    return (Path(__file__).with_name("submit_wrapper.sh")).read_text(encoding="utf-8")


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
    max_effective_submits = re.sub(
        r"[^0-9]", "",
        str(env.get(MAX_EFFECTIVE_SUBMITS_ENV) or os.getenv(MAX_EFFECTIVE_SUBMITS_ENV, "")),
    )
    if max_effective_submits:
        (state_dir / "max_effective_submits").write_text(
            max_effective_submits + "\n", encoding="utf-8"
        )

    submit_path = workspace / "submit.sh"
    original_submit_path = workspace / ".cybergym_submit.sh"
    wrapper_installed = False
    if submit_path.is_file():
        if original_submit_path.exists():
            original_submit_path.unlink()
        submit_path.rename(original_submit_path)
        submit_path.write_text(_submit_wrapper_text(), encoding="utf-8")
        submit_path.chmod(0o755)
        wrapper_installed = True

    readme = workspace / "README.md"
    if readme.is_file():
        original = readme.read_text(encoding="utf-8", errors="replace")
        readme.write_text(original + readme_append(sample_id), encoding="utf-8")

    return packet_metadata(source, copied_helpers, wrapper_installed, sample_id)
