"""Shared command contract for reward-framework harness adapters."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_FILE = REPO_ROOT / "reward_framework" / "prompt.txt"
DEFAULT_RUNS_ROOT = REPO_ROOT / "reward_framework" / "harness_runs"
DEFAULT_SKILL_PACKET = (
    REPO_ROOT
    / "reward_framework"
    / "offline_static_distillation"
    / "templates"
    / "skill_packet"
)
SERVER_ROOT = REPO_ROOT / "harness_runtime" / "server"
OPENHANDS_RUNNER = REPO_ROOT / "harness_runtime" / "openhands" / "arvo.py"
OPENHANDS_LOCAL_RUNNER = REPO_ROOT / "harness_runtime" / "openhands" / "local.py"
CLI_RUNNER = REPO_ROOT / "harness_runtime" / "cli.py"
DSH_RUNNER = REPO_ROOT / "harness_runtime" / "deepseek_harness" / "arvo.py"
DSH_LOCAL_RUNNER = REPO_ROOT / "harness_runtime" / "deepseek_harness" / "local.py"

SKILL_PACKET_ENV = "REWARD_FRAMEWORK_SKILL_PACKET_DIR"
MAX_EFFECTIVE_SUBMITS_ENV = "REWARD_FRAMEWORK_MAX_EFFECTIVE_SUBMITS"


def runner_python() -> str:
    candidates = [
        REPO_ROOT / "external" / "OpenHands" / ".venv-openhands" / "bin" / "python",
        *sorted(
            Path.home().glob(".cache/pypoetry/virtualenvs/openhands-ai-*/bin/python"),
            reverse=True,
        ),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return str(Path(sys.executable))


@dataclass(frozen=True)
class RewardRequest:
    harness: str
    sample_id: str
    model: str
    run_id: str
    results_dir: Path
    skill_packet: Path = DEFAULT_SKILL_PACKET
    base_url: str = ""
    api_key_env: str = ""
    api_version: str = ""
    max_iter: int = 100
    max_attempts: int = 3
    timeout: int = 10800
    server: str = "http://host.docker.internal:8666"
    difficulty: str = "level1"
    openhands_repo: Path | None = None
    max_effective_submits: int | None = None
    reasoning_effort: str = "max"
    max_output_tokens: int = 4096
    extra_args: tuple[str, ...] = ()

    @property
    def is_arvo(self) -> bool:
        return self.sample_id.startswith("arvo_")

    @property
    def arvo_id(self) -> str:
        if not self.is_arvo:
            raise ValueError(f"not an ARVO sample: {self.sample_id}")
        return self.sample_id[len("arvo_") :]


@dataclass(frozen=True)
class RewardCommand:
    harness: str
    command: tuple[str, ...]
    cwd: Path = REPO_ROOT
    env: Mapping[str, str] = field(default_factory=dict)

    def redacted(self) -> dict:
        return {
            "harness": self.harness,
            "command": list(self.command),
            "cwd": str(self.cwd),
            "env_keys": sorted(
                key
                for key in self.env
                if key.startswith(("REWARD_FRAMEWORK_", "HARNESS_", "CODEX_", "CLAUDE_", "DSH_"))
            ),
        }


def reward_environment(request: RewardRequest) -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CYBERGYM_OPENHANDS_"):
            env.pop(name, None)
    env[SKILL_PACKET_ENV] = str(request.skill_packet.expanduser().resolve())
    env["REWARD_FRAMEWORK_RUN_ID"] = request.run_id
    env["REWARD_FRAMEWORK_HARNESS"] = request.harness
    env["REWARD_FRAMEWORK_SAMPLE_ID"] = request.sample_id
    env["HARNESS_TASK_PROMPT_FILE"] = str(PROMPT_FILE)
    if request.max_effective_submits is not None:
        env[MAX_EFFECTIVE_SUBMITS_ENV] = str(request.max_effective_submits)
    return env


def common_args(request: RewardRequest) -> list[str]:
    args = [
        "--model", request.model,
        "--base-url", request.base_url,
        "--max-iter", str(request.max_iter),
        "--timeout", str(request.timeout),
        "--results-dir", str(request.results_dir),
        "--prompt-file", str(PROMPT_FILE),
    ]
    if request.api_key_env:
        args += ["--api-key-env", request.api_key_env]
    if request.api_version:
        args += ["--api-version", request.api_version]
    return args


def arvo_args(request: RewardRequest) -> list[str]:
    return [
        "--arvo-id", request.arvo_id,
        "--max-attempts", str(request.max_attempts),
        "--server", request.server,
        "--difficulty", request.difficulty,
        "--server-root", str(SERVER_ROOT),
    ]
