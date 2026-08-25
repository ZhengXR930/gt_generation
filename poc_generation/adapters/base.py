"""Command contract shared by baseline harness adapters."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_FILE = REPO_ROOT / "poc_generation" / "prompt.txt"
RESULTS_ROOT = REPO_ROOT / "poc_generation" / "poc_results"
SERVER_ROOT = REPO_ROOT / "harness_runtime" / "server"
OPENHANDS_RUNNER = REPO_ROOT / "harness_runtime" / "openhands" / "arvo.py"
OPENHANDS_LOCAL_RUNNER = REPO_ROOT / "harness_runtime" / "openhands" / "local.py"
CLI_RUNNER = REPO_ROOT / "harness_runtime" / "cli.py"
DSH_RUNNER = REPO_ROOT / "harness_runtime" / "deepseek_harness" / "arvo.py"
DSH_LOCAL_RUNNER = REPO_ROOT / "harness_runtime" / "deepseek_harness" / "local.py"


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
class Request:
    harness: str
    sample_id: str
    model: str
    namespace: str
    base_url: str = ""
    api_key_env: str = ""
    api_version: str = ""
    max_iter: int = 100
    max_attempts: int = 3
    timeout: int = 10800
    server: str = "http://host.docker.internal:8666"
    difficulty: str = "level1"
    openhands_repo: Path | None = None
    extra_args: tuple[str, ...] = ()

    @property
    def results_dir(self) -> Path:
        return RESULTS_ROOT / self.namespace

    @property
    def is_arvo(self) -> bool:
        return self.sample_id.startswith("arvo_")

    @property
    def arvo_id(self) -> str:
        if not self.is_arvo:
            raise ValueError(f"not an ARVO sample: {self.sample_id}")
        return self.sample_id[len("arvo_") :]


@dataclass(frozen=True)
class Command:
    harness: str
    command: tuple[str, ...]
    cwd: Path = REPO_ROOT
    env: Mapping[str, str] = field(default_factory=dict)

    def redacted(self) -> dict:
        return {"harness": self.harness, "command": list(self.command), "cwd": str(self.cwd)}


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("REWARD_FRAMEWORK_") or name in {
            "CYBERGYM_OPENHANDS_SKILL_PACKET_DIR",
            "CYBERGYM_MAX_EFFECTIVE_SUBMITS",
            "HARNESS_WORKSPACE_INSTALLER",
            "HARNESS_DSH_PATCH_FILE",
        }:
            env.pop(name, None)
    env["HARNESS_TASK_PROMPT_FILE"] = str(PROMPT_FILE)
    _extend_no_proxy(env, [".tiktok-row.net", "aidp-i18ntt-sg.tiktok-row.net"])
    return env


def _extend_no_proxy(env: dict[str, str], hosts: list[str]) -> None:
    existing = []
    for key in ("NO_PROXY", "no_proxy"):
        existing.extend(
            item.strip()
            for item in str(env.get(key) or "").split(",")
            if item.strip()
        )
    values = list(dict.fromkeys([*existing, *hosts]))
    joined = ",".join(values)
    env["NO_PROXY"] = joined
    env["no_proxy"] = joined


def common_args(request: Request) -> list[str]:
    args = [
        "--model", request.model, "--base-url", request.base_url,
        "--max-iter", str(request.max_iter), "--timeout", str(request.timeout),
        "--results-dir", str(request.results_dir), "--prompt-file", str(PROMPT_FILE),
    ]
    if request.api_key_env:
        args += ["--api-key-env", request.api_key_env]
    if request.api_version:
        args += ["--api-version", request.api_version]
    return args


def arvo_args(request: Request) -> list[str]:
    return [
        "--arvo-id", request.arvo_id,
        "--max-attempts", str(request.max_attempts),
        "--server", request.server,
        "--difficulty", request.difficulty,
        "--server-root", str(SERVER_ROOT),
    ]
