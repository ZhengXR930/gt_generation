"""Unified PoC-generation harness adapter registry.

This module is the boundary between:

- baseline PoC generation under ``poc_generation/poc_results``; and
- reward-framework skill experiments under an explicitly supplied result root.

The adapter registry owns harness-specific command construction.  Callers should
not special-case OpenHands/Codex/Claude/DeepSeek in batch launchers.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
POC_GENERATOR = REPO_ROOT / "poc_generation" / "poc_generator"
RUN_OPENHANDS_SAMPLE = POC_GENERATOR / "run_sample.py"
RUN_CLI_SAMPLE = POC_GENERATOR / "run_cli_sample.py"

OPENHANDS_SKILL_ENV = "CYBERGYM_OPENHANDS_SKILL_PACKET_DIR"
OPENHANDS_SUBMIT_BUDGET_ENV = "CYBERGYM_MAX_EFFECTIVE_SUBMITS"
GENERIC_SKILL_ENV = "REWARD_FRAMEWORK_POC_SKILL_PACKET_DIR"


def _repo_python() -> str:
    """Use the repo-managed Python when present; otherwise keep the caller one."""
    candidate = REPO_ROOT / "external" / "OpenHands" / ".venv-openhands" / "bin" / "python"
    return str(candidate if candidate.exists() else Path(sys.executable))


def normalize_harness_name(value: str | None) -> str:
    raw = str(value or "openhands").strip().lower().replace("-", "_")
    aliases = {
        "open_hands": "openhands",
        "oh": "openhands",
        "claude_code": "claude",
        "claude_cli": "claude",
        "dsh": "deepseek_harness",
        "deepseek": "deepseek_harness",
        "deepseek_harness": "deepseek_harness",
        "deepseek_harness_cli": "deepseek_harness",
    }
    return aliases.get(raw, raw)


@dataclass(frozen=True)
class PocHarnessRequest:
    harness: str
    arvo_id: str
    model: str
    base_url: str
    api_key_env: str
    max_iter: int
    max_attempts: int
    timeout: int
    server: str
    difficulty: str
    results_dir: Path
    openhands_repo: Path | None = None
    skill_packet_dir: Path | None = None
    api_version: str = ""

    @property
    def sample_id(self) -> str:
        return f"arvo_{self.arvo_id}"

    @property
    def normalized_harness(self) -> str:
        return normalize_harness_name(self.harness)


@dataclass(frozen=True)
class PocHarnessCommand:
    harness: str
    sample_id: str
    command: list[str]
    cwd: Path
    env: dict[str, str]
    results_dir: Path
    supports_skill_packet: bool
    runner: str

    def redacted(self) -> dict:
        prefixes = (
            "CYBERGYM_",
            "REWARD_FRAMEWORK_",
            "OPENHANDS_",
            "CODEX_",
            "CLAUDE_",
            "ANTHROPIC_",
            "DEEPSEEK_",
            "DSH_",
        )
        env_keys = sorted(k for k in self.env if k.startswith(prefixes))
        return {
            "harness": self.harness,
            "sample_id": self.sample_id,
            "command": self.command,
            "cwd": str(self.cwd),
            "env_keys": env_keys,
            "results_dir": str(self.results_dir),
            "supports_skill_packet": self.supports_skill_packet,
            "runner": self.runner,
        }


class PocHarnessAdapter:
    name = ""
    supports_skill_packet = False
    runner = ""

    def build_command(self, request: PocHarnessRequest) -> PocHarnessCommand:
        raise NotImplementedError

    def _base_env(self, request: PocHarnessRequest) -> dict[str, str]:
        env = os.environ.copy()
        # Baseline PoC generation must not accidentally inherit a reward skill
        # packet from an outer experiment.  Skill use is opt-in per request.
        for name in (
            OPENHANDS_SKILL_ENV,
            OPENHANDS_SUBMIT_BUDGET_ENV,
            GENERIC_SKILL_ENV,
            "REWARD_FRAMEWORK_CODEX_SKILLS_DIR",
            "REWARD_FRAMEWORK_CLAUDE_SKILLS_DIR",
            "REWARD_FRAMEWORK_DSH_BUNDLE_DIR",
        ):
            env.pop(name, None)
        if request.skill_packet_dir is not None:
            env[GENERIC_SKILL_ENV] = str(request.skill_packet_dir)
        return env


class OpenHandsPocAdapter(PocHarnessAdapter):
    name = "openhands"
    supports_skill_packet = True
    runner = "poc_generation.poc_generator.run_sample"

    def build_command(self, request: PocHarnessRequest) -> PocHarnessCommand:
        env = self._base_env(request)
        if request.skill_packet_dir is not None:
            env[OPENHANDS_SKILL_ENV] = str(request.skill_packet_dir)
        command = [
            _repo_python(),
            str(RUN_OPENHANDS_SAMPLE),
            "--arvo-id",
            request.arvo_id,
            "--model",
            request.model,
            "--base-url",
            request.base_url,
            "--max-iter",
            str(request.max_iter),
            "--max-attempts",
            str(request.max_attempts),
            "--timeout",
            str(request.timeout),
            "--server",
            request.server,
            "--difficulty",
            request.difficulty,
            "--results-dir",
            str(request.results_dir),
            "--harness-profile",
            "baseline",
        ]
        if request.openhands_repo:
            command += ["--openhands-repo", str(request.openhands_repo)]
        if request.api_version:
            command += ["--api-version", request.api_version]
        if request.api_key_env:
            command += ["--api-key-env", request.api_key_env]
        return PocHarnessCommand(
            harness=self.name,
            sample_id=request.sample_id,
            command=command,
            cwd=POC_GENERATOR,
            env=env,
            results_dir=request.results_dir,
            supports_skill_packet=self.supports_skill_packet,
            runner=self.runner,
        )


class CliAgentPocAdapter(PocHarnessAdapter):
    supports_skill_packet = True
    runner = "poc_generation.poc_generator.run_cli_sample"

    def build_command(self, request: PocHarnessRequest) -> PocHarnessCommand:
        env = self._base_env(request)
        command = [
            _repo_python(),
            str(RUN_CLI_SAMPLE),
            "--harness",
            self.name,
            "--arvo-id",
            request.arvo_id,
            "--model",
            request.model,
            "--base-url",
            request.base_url,
            "--max-iter",
            str(request.max_iter),
            "--max-attempts",
            str(request.max_attempts),
            "--timeout",
            str(request.timeout),
            "--server",
            request.server,
            "--difficulty",
            request.difficulty,
            "--results-dir",
            str(request.results_dir),
        ]
        if request.api_key_env:
            command += ["--api-key-env", request.api_key_env]
        if request.skill_packet_dir is not None:
            command += ["--skill-packet-dir", str(request.skill_packet_dir)]
        return PocHarnessCommand(
            harness=self.name,
            sample_id=request.sample_id,
            command=command,
            cwd=POC_GENERATOR,
            env=env,
            results_dir=request.results_dir,
            supports_skill_packet=self.supports_skill_packet,
            runner=self.runner,
        )


class CodexPocAdapter(CliAgentPocAdapter):
    name = "codex"


class ClaudePocAdapter(CliAgentPocAdapter):
    name = "claude"


class DeepSeekHarnessPocAdapter(CliAgentPocAdapter):
    name = "deepseek_harness"


_ADAPTERS: dict[str, PocHarnessAdapter] = {
    "openhands": OpenHandsPocAdapter(),
    "codex": CodexPocAdapter(),
    "claude": ClaudePocAdapter(),
    "deepseek_harness": DeepSeekHarnessPocAdapter(),
}


def supported_harnesses() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def get_poc_harness_adapter(name: str | None) -> PocHarnessAdapter:
    normalized = normalize_harness_name(name)
    try:
        return _ADAPTERS[normalized]
    except KeyError as exc:
        raise ValueError(
            f"unsupported PoC harness {name!r}; supported: {', '.join(supported_harnesses())}"
        ) from exc


def build_poc_harness_command(request: PocHarnessRequest) -> PocHarnessCommand:
    return get_poc_harness_adapter(request.harness).build_command(request)
