"""Minimal cross-platform control surface used by the orchestrator."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Protocol


class PlatformAdapter(Protocol):
    platform_name: str
    workspace_root: Path

    def inject_message(self, message: str) -> None: ...
    def checkpoint(self, label: str) -> Path | None: ...
    def submission_ready(self) -> bool: ...
    def submission_fingerprint(self) -> str | None: ...


class CallbackAdapter:
    platform_name = "generic"

    def __init__(self, *, workspace_root: Path,
                 inject: Callable[[str], None] | None = None,
                 checkpoint_callback: Callable[[str], Path | None] | None = None,
                 submission_ready_callback: Callable[[], bool] | None = None):
        self.workspace_root = workspace_root.resolve()
        self._inject = inject or (lambda _message: None)
        self._checkpoint = checkpoint_callback or (lambda _label: None)
        self._submission_ready = submission_ready_callback or (lambda: True)

    def inject_message(self, message: str) -> None:
        self._inject(message)

    def checkpoint(self, label: str) -> Path | None:
        return self._checkpoint(label)

    def submission_ready(self) -> bool:
        return self._submission_ready()

    def submission_fingerprint(self) -> str | None:
        """Identify the currently materialized candidate without interpreting it."""
        candidate = self.workspace_root / "poc.bin"
        if not candidate.is_file():
            return None
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
