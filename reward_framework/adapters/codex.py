"""Codex-platform callback adapter.

Codex integrations can map tool-call events to ``handle_submission`` and inject
the returned factual feedback as the next user/tool observation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .base import CallbackAdapter


class CodexAdapter(CallbackAdapter):
    platform_name = "codex"

    def __init__(self, *, workspace_root: Path,
                 inject: Callable[[str], None],
                 checkpoint_callback: Callable[[str], Path | None]):
        super().__init__(workspace_root=workspace_root, inject=inject,
                         checkpoint_callback=checkpoint_callback)
