"""Minimal cross-platform control surface used by the orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol


class PlatformAdapter(Protocol):
    workspace_root: Path

    def inject_message(self, message: str) -> None: ...
    def checkpoint(self, label: str) -> Path | None: ...


class CallbackAdapter:
    def __init__(self, *, workspace_root: Path,
                 inject: Callable[[str], None] | None = None,
                 checkpoint_callback: Callable[[str], Path | None] | None = None):
        self.workspace_root = workspace_root.resolve()
        self._inject = inject or (lambda _message: None)
        self._checkpoint = checkpoint_callback or (lambda _label: None)

    def inject_message(self, message: str) -> None:
        self._inject(message)

    def checkpoint(self, label: str) -> Path | None:
        return self._checkpoint(label)
