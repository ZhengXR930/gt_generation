"""Prompt/README bridge for exposing OpenHands skill packets."""

from __future__ import annotations

from .contract import readme_append, workspace_bootstrap_command


def build_workspace_readme_appendix(sample_id: str) -> str:
    """Return the adapter-owned README appendix for a generated workspace."""
    return readme_append(sample_id)


def build_workspace_bootstrap_command() -> str:
    """Return the read-only bootstrap command used by controller recovery."""
    return workspace_bootstrap_command()
