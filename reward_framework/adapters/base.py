"""Shared interfaces for harness adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AdapterInstallResult:
    """Metadata returned after installing a skill packet into a workspace."""

    adapter: str
    interface_version: str
    source: str
    workspace_packet_dir: str
    workspace_helpers_dir: str
    workspace_state_dir: str
    helpers: list[str]
    submit_wrapper_installed: bool
    sample_id: str


class HarnessAdapter(Protocol):
    """Minimal contract expected from a harness adapter."""

    name: str
    interface_version: str

    def install_workspace_skill_packet(
        self, task_dir: Path, task_id: str
    ) -> AdapterInstallResult | None:
        """Install a skill packet into a generated task workspace."""

    def validate_skill_packet(self, packet: Path) -> None:
        """Raise on interface violations before running model tests."""
