"""Neutral Codex subsession executor.

The benchmark runner in `cli.py` drives a coding agent through a full sample
workspace. Distillation needs the same agent for a different shape of work:
give a role a directory of inputs, let it read them with real tools, and have
it write one JSON artifact back. That is the same execution primitive without
the benchmark scaffolding, so it lives here and `cli.py` delegates to it.

Nothing in this module knows about samples, ARVO, submit.sh, or skill packets.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from harness_runtime.auth import default_api_key_env, load_env_key

GT_ROOT = Path(__file__).resolve().parents[1]
CODEX_BRIDGE = GT_ROOT / "gt_generation" / "adapters" / "codex" / "modelhub_crawl_bridge.py"

BRIDGE_READY_TIMEOUT = 15.0


def load_api_key(env: MutableMapping[str, str], key_env: str) -> None:
    """Populate the API key env var without ever logging the secret."""
    if not key_env or env.get(key_env):
        return
    try:
        env[key_env] = load_env_key(key_env)
    except Exception:
        # Let the underlying CLI emit its native auth error. Manifests and logs
        # still show the env key name, not the secret.
        return


def bridge_target_url(base_url: str, api_version: str = "") -> str:
    target, _separator, query = str(base_url or "").strip().partition("?")
    target = target.rstrip("/")
    if not target:
        return ""
    clean_url = target.lower()
    if not clean_url.endswith("/messages") and not clean_url.endswith("/chat/completions"):
        target = f"{target}/chat/completions"
        clean_url = target.lower()
    if clean_url.endswith("/chat/completions") and api_version and "api-version=" not in query.lower():
        query = f"{query}&api-version={api_version}" if query else f"api-version={api_version}"
    return target + (f"?{query}" if query else "")


def should_start_bridge(base_url: str, bridge: str = "auto") -> bool:
    if not base_url:
        return False
    choice = str(bridge or "auto").strip().lower()
    if choice in {"none", "off", "disabled"}:
        return False
    if choice == "modelhub_crawl":
        return True
    if choice not in {"", "auto"}:
        raise ValueError(
            "codex bridge must be one of auto, none, or modelhub_crawl; " f"got {bridge!r}"
        )
    clean_url = base_url.split("?", 1)[0].rstrip("/").lower()
    return (
        "modelhub" in clean_url
        or "/crawl/" in clean_url
        or clean_url.endswith("/messages")
        or clean_url.endswith("/chat/completions")
    )


def start_modelhub_bridge(
    *,
    base_url: str,
    api_version: str,
    api_key_env: str,
    model: str,
    bridge_dir: Path,
    env: MutableMapping[str, str],
    payload_format: str = "auto",
    max_output_tokens: int = 4096,
    timeout: int = 1800,
    caller: str = "",
    disable_proxy: bool = False,
) -> tuple[subprocess.Popen | None, str]:
    """Start the ModelHub bridge and return (process, local base url)."""
    if not CODEX_BRIDGE.is_file():
        raise FileNotFoundError(f"missing Codex ModelHub bridge: {CODEX_BRIDGE}")
    bridge_dir.mkdir(parents=True, exist_ok=True)
    port_file = bridge_dir / "port"
    log_file = bridge_dir / "bridge.log"
    if port_file.exists():
        port_file.unlink()

    key_env = api_key_env or default_api_key_env(model)
    load_api_key(env, key_env)
    cmd = [
        sys.executable,
        str(CODEX_BRIDGE),
        "--host", "127.0.0.1",
        "--port", "0",
        "--port-file", str(port_file),
        "--target-url", bridge_target_url(base_url, api_version),
        "--api-key-env", key_env,
        "--payload-format", str(payload_format or "auto"),
        "--max-tokens", str(max_output_tokens),
        "--timeout-seconds", str(timeout),
        "--log-file", str(log_file),
    ]
    if caller:
        cmd += ["--caller", caller]
    if disable_proxy:
        cmd += ["--disable-proxy"]

    stream = log_file.open("a", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=GT_ROOT, env=dict(env), stdout=stream, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + BRIDGE_READY_TIMEOUT
    while time.monotonic() < deadline:
        if port_file.is_file():
            port = port_file.read_text(encoding="utf-8").strip()
            if port:
                return proc, f"http://127.0.0.1:{port}"
        if proc.poll() is not None:
            raise RuntimeError(f"Codex ModelHub bridge exited early; see {log_file}")
        time.sleep(0.05)
    raise RuntimeError(f"Codex ModelHub bridge did not become ready; see {log_file}")


def codex_binary(env: Mapping[str, str] | None = None) -> str:
    env_map = env or os.environ
    configured = str(env_map.get("CODEX_BIN") or "").strip()
    if configured:
        return configured
    found = shutil.which("codex", path=env_map.get("PATH"))
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "codex"
    if fallback.is_file():
        return str(fallback)
    return "codex"


def _prepend_path(env: MutableMapping[str, str], directory: Path) -> None:
    current_path = env.get("PATH") or os.defpath
    parts = current_path.split(os.pathsep) if current_path else []
    directory_text = str(directory)
    if directory_text not in parts:
        env["PATH"] = os.pathsep.join([directory_text, *parts])


def _write_cli_shell_profile(home: Path, tools_dir: Path) -> None:
    """Make repo CLI shims visible to login and non-login Bash tool shells."""
    home.mkdir(parents=True, exist_ok=True)
    line = f'export PATH="{tools_dir}:${{PATH:-/usr/local/bin:/usr/bin:/bin}}"\n'
    marker_begin = "# >>> gt_generation cli tool shims >>>\n"
    marker_end = "# <<< gt_generation cli tool shims <<<\n"
    block = marker_begin + line + marker_end

    for name in (".bash_profile", ".bash_login", ".profile", ".bashrc"):
        profile = home / name
        text = profile.read_text(encoding="utf-8") if profile.is_file() else ""
        start = text.find(marker_begin)
        end = text.find(marker_end)
        if start >= 0 and end >= start:
            end += len(marker_end)
            text = text[:start] + block + text[end:]
        elif text:
            suffix = "" if text.endswith("\n") else "\n"
            text = text + suffix + block
        else:
            text = block
        profile.write_text(text, encoding="utf-8")

    bash_env = home / ".bash_env"
    bash_env.write_text(line, encoding="utf-8")


def install_cli_tool_shims(env: MutableMapping[str, str]) -> None:
    """Expose repo-provided CLI compatibility tools to host-side agents.

    Codex tools execute Bash as a login shell in some environments. A PATH-only
    parent-process fix is not enough because login startup files may reset PATH,
    so Codex runs get an isolated HOME containing shell startup files that also
    prepend the repo shim directory.
    """
    tools_dir = GT_ROOT / "harness_runtime" / "cli_tools"
    apply_patch = tools_dir / "apply_patch"
    if not apply_patch.is_file():
        return
    try:
        apply_patch.chmod(0o755)
    except OSError:
        pass
    _prepend_path(env, tools_dir)

    codex_home = str(env.get("CODEX_HOME") or "").strip()
    if codex_home:
        home = Path(codex_home).expanduser()
        env["HOME"] = str(home)
        _write_cli_shell_profile(home, tools_dir)
        env["BASH_ENV"] = str(home / ".bash_env")


def codex_exec_command(
    *,
    workspace: Path,
    prompt: str,
    model: str,
    provider_base: str,
    api_key_env: str,
    env: MutableMapping[str, str],
    reasoning_effort: str = "",
    bridged: bool = False,
) -> list[str]:
    """Build the `codex exec` argv used for both benchmark and role sessions."""
    install_cli_tool_shims(env)
    command = [
        codex_binary(env),
        "exec",
        "--cd", str(workspace),
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "--strict-config",
    ]
    if provider_base:
        provider = "modelhub" if bridged else "custom"
        key_env = api_key_env or default_api_key_env(model)
        load_api_key(env, key_env)
        command += [
            "-c", f'model_provider="{provider}"',
            "-c", f'model_providers.{provider}.name="{provider}"',
            "-c", f'model_providers.{provider}.base_url="{provider_base}"',
            "-c", f'model_providers.{provider}.wire_api="responses"',
            "-c", f'model_providers.{provider}.env_key="{key_env}"',
        ]
    if model:
        command += ["-m", model]
    if reasoning_effort:
        command += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    command.append(prompt)
    return command


@dataclass(frozen=True)
class SubsessionSpec:
    """One role invocation: a directory of inputs in, one JSON artifact out."""

    name: str
    workspace: Path
    prompt: str
    output_file: Path
    model: str
    base_url: str = ""
    api_key_env: str = ""
    api_version: str = ""
    reasoning_effort: str = "max"
    max_output_tokens: int = 4096
    timeout: int = 1800
    bridge: str = "auto"
    bridge_payload_format: str = "chat_completions"
    bridge_disable_proxy: bool = True
    log_dir: Path | None = None
    extra_env: Mapping[str, str] = field(default_factory=dict)


def run_codex_subsession(spec: SubsessionSpec, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run one role as a Codex subsession and report where its artifact landed."""
    run_env: dict[str, str] = dict(env if env is not None else os.environ)
    run_env.update(spec.extra_env)
    log_dir = spec.log_dir or (spec.workspace / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    bridge_proc: subprocess.Popen | None = None
    provider_base = spec.base_url
    bridged = False
    if should_start_bridge(spec.base_url, spec.bridge):
        bridge_proc, provider_base = start_modelhub_bridge(
            base_url=spec.base_url,
            api_version=spec.api_version,
            api_key_env=spec.api_key_env,
            model=spec.model,
            bridge_dir=log_dir / "codex_modelhub_bridge",
            env=run_env,
            payload_format=spec.bridge_payload_format,
            max_output_tokens=spec.max_output_tokens,
            timeout=spec.timeout,
            caller=spec.name,
            disable_proxy=spec.bridge_disable_proxy,
        )
        bridged = True

    command = codex_exec_command(
        workspace=spec.workspace,
        prompt=spec.prompt,
        model=spec.model,
        provider_base=provider_base,
        api_key_env=spec.api_key_env,
        env=run_env,
        reasoning_effort=spec.reasoning_effort,
        bridged=bridged,
    )

    stdout_path = log_dir / f"{spec.name}.stdout.txt"
    status = "ok"
    returncode: int | None = None
    try:
        with stdout_path.open("w", encoding="utf-8") as stream:
            proc = subprocess.run(
                command,
                cwd=str(spec.workspace),
                env=run_env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=spec.timeout,
            )
        returncode = proc.returncode
        if returncode != 0:
            status = "agent_failed"
    except subprocess.TimeoutExpired:
        status = "timeout"
    finally:
        if bridge_proc is not None and bridge_proc.poll() is None:
            bridge_proc.terminate()
            try:
                bridge_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bridge_proc.kill()

    if status == "ok" and not spec.output_file.is_file():
        status = "missing_output"

    return {
        "subsession": spec.name,
        "status": status,
        "returncode": returncode,
        "workspace": str(spec.workspace),
        "output_file": str(spec.output_file),
        "stdout": str(stdout_path),
        "command": command[:-1] + ["<prompt>"],
    }
