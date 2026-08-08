"""Model backend abstraction and bounded, persistent Codex CLI sessions."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class RewardAgentBackend(Protocol):
    model: str

    def run_json(self, *, role: str, prompt: str, schema: Path,
                 cwd: Path) -> dict[str, Any]: ...


@dataclass
class CodexRun:
    role: str
    session_id: str | None
    resumed: bool
    commands: list[str]
    usage: dict[str, Any]


class CodexBackend:
    """One durable Codex CLI role.

    A backend instance represents one agent identity.  All calls made through
    it resume the same Codex thread; Reward Agent and Harness Patcher therefore
    use two instances and never accidentally share conversational memory.
    """

    _FORBIDDEN_COMMAND = re.compile(
        r"(^|[;&|]\s*)(cd\s+\.\.|find\s+\.\.|ls\s+\.\.|"
        r"env\b|printenv\b|curl\b|wget\b|git\s+(show|log|diff)\b)|"
        r"(/home/|/root/|gt_results|poc_results|sanitizer_trace|patch\.diff)",
        re.IGNORECASE,
    )

    def __init__(self, *, model: str = "gpt-5.5", executable: str = "codex",
                 timeout: int = 1800, session_file: Path | None = None,
                 sandbox: str = "read-only"):
        if sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("CodexBackend only permits read-only or workspace-write")
        self.model = model
        self.executable = executable
        self.timeout = timeout
        self.session_file = session_file.resolve() if session_file else None
        self.sandbox = sandbox
        self.session_id = self._load_session_id()
        self.runs: list[CodexRun] = []

    def _load_session_id(self) -> str | None:
        if self.session_file is None or not self.session_file.is_file():
            return None
        value = self.session_file.read_text(encoding="utf-8").strip()
        return value or None

    def _save_session_id(self, session_id: str) -> None:
        self.session_id = session_id
        if self.session_file is None:
            return
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.session_file.with_suffix(".tmp")
        temporary.write_text(session_id + "\n", encoding="utf-8")
        temporary.replace(self.session_file)

    def run_json(self, *, role: str, prompt: str, schema: Path,
                 cwd: Path) -> dict[str, Any]:
        cwd = cwd.resolve()
        if not cwd.is_dir() or not schema.is_file():
            raise FileNotFoundError("Codex cwd and output schema must exist")
        with tempfile.TemporaryDirectory(prefix=f"reward-agent-{role}-") as raw:
            result = Path(raw) / "result.json"
            resumed = self.session_id is not None
            if resumed:
                command = [
                    self.executable, "exec", "resume",
                    "--model", self.model,
                    "--skip-git-repo-check",
                    "--ignore-user-config", "--ignore-rules",
                    "--output-schema", str(schema.resolve()),
                    "--output-last-message", str(result),
                    "--json", str(self.session_id), "-",
                ]
            else:
                command = [
                    self.executable, "exec",
                    "--model", self.model,
                    "--sandbox", self.sandbox,
                    "--cd", str(cwd),
                    "--skip-git-repo-check",
                    "--ignore-user-config", "--ignore-rules",
                    "--output-schema", str(schema.resolve()),
                    "--output-last-message", str(result),
                    "--json", "-",
                ]
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                # `codex exec resume` has no --cd option. Without an explicit
                # process cwd, resumed Reward/Patcher turns silently inherit
                # the controller's directory and can no longer see their
                # materialized state view.
                cwd=cwd,
                timeout=self.timeout,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr[-4000:] or completed.stdout[-4000:]
                raise RuntimeError(
                    f"Codex role {role} exited {completed.returncode}: {detail}"
                )
            commands: list[str] = []
            usage: dict[str, Any] = {}
            thread_id: str | None = None
            for line in completed.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") in {"thread.started", "session.started"}:
                    thread_id = str(
                        event.get("thread_id") or event.get("session_id") or ""
                    ).strip() or None
                elif event.get("type") == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "command_execution":
                        commands.append(str(item.get("command") or ""))
                elif event.get("type") == "turn.completed":
                    usage = dict(event.get("usage") or {})
            violations = [cmd for cmd in commands if self._FORBIDDEN_COMMAND.search(cmd)]
            if not resumed:
                if thread_id is None:
                    raise RuntimeError(
                        f"Codex role {role} did not report a persistent thread id"
                    )
                self._save_session_id(thread_id)
            self.runs.append(CodexRun(
                role, self.session_id, resumed, commands, usage
            ))
            if violations:
                raise RuntimeError(
                    f"Codex role {role} violated its information boundary: "
                    + "; ".join(violations[:3])
                )
            if not result.is_file():
                raise RuntimeError(f"Codex role {role} produced no structured result")
            value = json.loads(result.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"Codex role {role} result is not an object")
            return value
