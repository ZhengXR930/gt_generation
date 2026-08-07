"""Model backend abstraction and a bounded Codex CLI implementation."""

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
    commands: list[str]
    usage: dict[str, Any]


class CodexBackend:
    """Invoke an ephemeral, read-only Codex session for one Reward-Agent role."""

    _FORBIDDEN_COMMAND = re.compile(
        r"(^|[;&|]\s*)(cd\s+\.\.|find\s+\.\.|ls\s+\.\.|"
        r"env\b|printenv\b|curl\b|wget\b|git\s+(show|log|diff)\b)|"
        r"(/home/|/root/|gt_results|poc_results|sanitizer_trace|patch\.diff)",
        re.IGNORECASE,
    )

    def __init__(self, *, model: str = "gpt-5.5", executable: str = "codex",
                 timeout: int = 1800):
        self.model = model
        self.executable = executable
        self.timeout = timeout
        self.runs: list[CodexRun] = []

    def run_json(self, *, role: str, prompt: str, schema: Path,
                 cwd: Path) -> dict[str, Any]:
        cwd = cwd.resolve()
        if not cwd.is_dir() or not schema.is_file():
            raise FileNotFoundError("Codex cwd and output schema must exist")
        with tempfile.TemporaryDirectory(prefix=f"reward-agent-{role}-") as raw:
            result = Path(raw) / "result.json"
            command = [
                self.executable, "exec",
                "--model", self.model,
                "--sandbox", "read-only",
                "--cd", str(cwd),
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--output-schema", str(schema.resolve()),
                "--output-last-message", str(result),
                "--json", "-",
            ]
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
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
            for line in completed.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "command_execution":
                        commands.append(str(item.get("command") or ""))
                elif event.get("type") == "turn.completed":
                    usage = dict(event.get("usage") or {})
            violations = [cmd for cmd in commands if self._FORBIDDEN_COMMAND.search(cmd)]
            self.runs.append(CodexRun(role, commands, usage))
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
