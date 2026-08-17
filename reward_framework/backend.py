"""Model backend abstraction and filesystem-isolated Codex CLI turns."""

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
    """Filesystem-isolated Codex CLI backend for Reward Agent roles."""

    _FORBIDDEN_COMMAND = re.compile(
        r"(^|[;&|]\s*)(cd\s+\.\.|find\s+\.\.|ls\s+\.\.|"
        r"env\b|printenv\b|curl\b|wget\b|git\s+(show|log|diff)\b)|"
        r"(/home/|/root/|gt_results|poc_results|sanitizer_trace|patch\.diff)",
        re.IGNORECASE,
    )

    def __init__(self, *, model: str = "gpt-5.5", executable: str = "codex",
                 timeout: int = 1800, session_file: Path | None = None,
                 sandbox: str = "read-only", fresh_each_run: bool = False,
                 isolation_image: str | None = None,
                 isolation_auth_file: Path | None = None):
        if sandbox != "read-only":
            raise ValueError("Reward CodexBackend permits only read-only sandbox")
        self.model = model
        self.executable = executable
        self.timeout = timeout
        self.session_file = session_file.resolve() if session_file else None
        self.sandbox = sandbox
        self.fresh_each_run = fresh_each_run
        self.isolation_image = isolation_image
        self.isolation_auth_file = (
            isolation_auth_file.resolve() if isolation_auth_file else None
        )
        if self.isolation_image:
            if not self.fresh_each_run:
                raise ValueError(
                    "filesystem-isolated Codex turns require fresh_each_run=True"
                )
            if self.isolation_auth_file is None or not self.isolation_auth_file.is_file():
                raise FileNotFoundError(
                    "filesystem-isolated Codex requires a host-visible auth.json"
                )
            executable_path = Path(self.executable).expanduser().resolve()
            if not executable_path.is_file():
                raise FileNotFoundError(
                    f"filesystem-isolated Codex executable is missing: {executable_path}"
                )
            self.executable = str(executable_path)
        self.session_id = self._load_session_id()
        self.runs: list[CodexRun] = []

    def _isolated_command(self, *, cwd: Path, schema: Path,
                          output_root: Path) -> list[str]:
        """Run Codex with no host data mounted except its explicit role view.

        ``--sandbox read-only`` alone is not a confidentiality boundary: it can
        still read paths outside ``cwd``.  The sibling container has no
        ``/home/xinran`` mount and no Docker socket.  It receives exactly the
        role view, output directory, schema, executable, and authentication
        file.  Thus GT paths do not exist in the model's filesystem namespace.
        """
        assert self.isolation_image is not None
        assert self.isolation_auth_file is not None
        workspace_mount = f"type=bind,src={cwd},dst=/work"
        if self.sandbox == "read-only":
            workspace_mount += ",readonly"
        return [
            "docker", "run", "--rm", "-i",
            # Codex needs network access only for its model call. It receives
            # neither the controller's internal evaluation network nor the
            # Docker socket.
            "--network", "bridge",
            "--workdir", "/work",
            "--tmpfs", "/root/.codex:rw,nosuid,size=64m",
            "--mount", (
                f"type=bind,src={self.isolation_auth_file},"
                "dst=/root/.codex/auth.json,readonly"
            ),
            "--mount", (
                f"type=bind,src={self.executable},"
                "dst=/usr/local/bin/codex,readonly"
            ),
            "--mount", workspace_mount,
            "--mount", f"type=bind,src={output_root},dst=/output",
            "--mount", (
                f"type=bind,src={schema.resolve()},dst=/schema.json,readonly"
            ),
            "--entrypoint", "/usr/local/bin/codex",
            self.isolation_image,
            "exec", "--model", self.model,
            # Docker is the confidentiality and write boundary. Asking Codex
            # to create a second bubblewrap namespace inside the container is
            # both redundant and unreliable (the standalone helper is not
            # present in the minimal controller image). The role view mount is
            # read-only.
            "--sandbox", "danger-full-access",
            "--cd", "/work",
            "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "--ephemeral",
            # The Reward Agent roles must not inherit account-level apps,
            # plugins, memories, goals, or auxiliary agents. Those channels
            # can carry information outside the explicit role view even when
            # the filesystem mount itself is isolated.
            "--disable", "apps",
            "--disable", "plugins",
            "--disable", "remote_plugin",
            "--disable", "plugin_sharing",
            "--disable", "goals",
            "--disable", "multi_agent",
            "--disable", "browser_use",
            "--disable", "browser_use_external",
            "--disable", "browser_use_full_cdp_access",
            "--disable", "computer_use",
            "--disable", "image_generation",
            "--disable", "tool_suggest",
            "--disable", "workspace_dependencies",
            "--disable", "hooks",
            "--output-schema", "/schema.json",
            "--output-last-message", "/output/result.json",
            "--json", "-",
        ]

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

    def reset_session(self) -> None:
        """Start the next role turn with a clean model context.

        Durable cross-episode state belongs in the controller-owned state
        files, not in an ever-growing model conversation.  Keeping the old
        remote thread is unnecessary; replacing only the small local thread
        pointer makes the next ``run_json`` create a fresh session while
        preserving every auditable input on disk.
        """
        self.session_id = None

    def run_json(self, *, role: str, prompt: str, schema: Path,
                 cwd: Path) -> dict[str, Any]:
        cwd = cwd.resolve()
        if not cwd.is_dir() or not schema.is_file():
            raise FileNotFoundError("Codex cwd and output schema must exist")
        if self.fresh_each_run:
            # Observation/evidence files are the durable Reward Agent state.
            # Replaying an unbounded conversation duplicates that state and can
            # fail in Codex's pre-sampling compaction before any probe is made.
            self.reset_session()
        with tempfile.TemporaryDirectory(prefix=f"reward-agent-{role}-") as raw:
            result = Path(raw) / "result.json"
            resumed = self.session_id is not None
            if self.isolation_image:
                # External JSON state is authoritative; isolated calls are
                # deliberately ephemeral and cannot resume a hidden thread.
                resumed = False
                command = self._isolated_command(
                    cwd=cwd, schema=schema, output_root=Path(raw).resolve()
                )
            elif resumed:
                command = [
                    self.executable, "exec", "resume",
                    "--model", self.model,
                    # `codex exec resume` does not expose `--sandbox`; without
                    # this override it silently falls back to read-only even
                    "-c", f'sandbox_mode="{self.sandbox}"',
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
                if self.fresh_each_run:
                    # The authoritative state is already materialized in cwd;
                    # do not leave one local Codex transcript per observer turn.
                    command.insert(-2, "--ephemeral")
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                # `codex exec resume` has no --cd option. Without an explicit
                # process cwd, resumed Reward Agent turns silently inherit
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
            sandbox_failures: list[str] = []
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
                        output = str(item.get("aggregated_output") or "")
                        if int(item.get("exit_code") or 0) != 0 and any(
                            marker in output.lower() for marker in (
                                "bwrap: no permissions to create a new namespace",
                                "operation not permitted creating new namespace",
                            )
                        ):
                            sandbox_failures.append(output[-1000:])
                elif event.get("type") == "turn.completed":
                    usage = dict(event.get("usage") or {})
            violations = [cmd for cmd in commands if self._FORBIDDEN_COMMAND.search(cmd)]
            if not resumed:
                if thread_id is None:
                    raise RuntimeError(
                        f"Codex role {role} did not report a persistent thread id"
                    )
                if self.fresh_each_run:
                    self.session_id = thread_id
                else:
                    self._save_session_id(thread_id)
            self.runs.append(CodexRun(
                role, self.session_id, resumed, commands, usage
            ))
            if sandbox_failures:
                raise RuntimeError(
                    f"Codex role {role} could not inspect its isolated worktree: "
                    + sandbox_failures[0]
                )
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
