#!/usr/bin/env python3
"""Run one ARVO CyberGym sample with a host-side CLI harness.

It creates the same public CyberGym workspace and submit.sh as the OpenHands
ARVO runtime, runs one CLI agent session from that workspace, then persists
submissions/analysis/manifest under the caller's ``--results-dir``.

Prompt ownership, result namespaces, and optional workspace augmentation belong
to the calling frontend.  This module is deliberately policy-neutral.
"""

def _ensure_repo_python() -> None:
    import sys as _sys
    from pathlib import Path as _Path

    if _sys.version_info >= (3, 11):
        return
    repo_root = _Path(__file__).resolve().parents[1]
    runtime_root = _Path(__file__).resolve().parent
    _sys.path.insert(0, str(runtime_root.parent))
    from harness_runtime.python_env import ensure_repo_python as _ensure  # noqa: PLC0415

    _ensure(repo_root, min_version=(3, 11))


_ensure_repo_python()

import argparse
import json
import logging
import os
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
GT_ROOT = ROOT.parent

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(GT_ROOT))
sys.path.insert(0, str(GT_ROOT / "external" / "cybergym" / "src"))

from cybergym.task.arvo_task import generate_arvo_task  # noqa: E402
from cybergym.task.types import TaskConfig, TaskDifficulty  # noqa: E402
from evaluator.reasoning.analysis_artifact import validate_analysis_artifact_quality  # noqa: E402
from harness_runtime.auth import default_api_key_env, load_env_key  # noqa: E402
from harness_runtime.failure_artifact import write_failure_artifact  # noqa: E402
from harness_runtime.submission_db import check as check_success  # noqa: E402
from harness_runtime.dedup import deduplicate_submission_attempts  # noqa: E402
from harness_runtime.openhands.arvo import (  # noqa: E402
    cleanup_scratch,
    clear_previous_result,
    ensure_arvo_source,
    persist_analysis_artifact,
    persist_submission_attempts,
)
from harness_runtime.openhands.local import (  # noqa: E402
    LocalExecutionBridge,
    check_runtime_readiness as check_local_runtime_readiness,
    clear_previous_result as clear_local_previous_result,
    prepare_workspace as prepare_local_workspace,
    validate_submissions_on_host,
    write_submit_sh as write_local_submit_sh,
)
from harness_runtime.workspace import (  # noqa: E402
    install_submit_candidate_guard,
    render_prompt,
    run_workspace_installer,
)


CODEX_BRIDGE = GT_ROOT / "gt_generation" / "adapters" / "codex" / "modelhub_crawl_bridge.py"
CLI_HARNESSES = ("codex", "claude")


def _scratch_root() -> Path:
    configured = os.environ.get("GT_GENERATION_SCRATCH_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else GT_ROOT.parent / ".cache" / "gt_generation_harness_runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _make_scratch(sample_id: str, harness: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"run_{sample_id}_{harness}_", dir=_scratch_root()))


def _cleanup_cli_scratch(scratch: Path) -> None:
    scratch = scratch.resolve()
    allowed_roots = {_scratch_root(), Path(tempfile.gettempdir()).resolve()}
    if scratch.parent not in allowed_roots or not scratch.name.startswith("run_"):
        logging.warning("Refusing to clean unexpected scratch path: %s", scratch)
        return
    try:
        shutil.rmtree(scratch)
        return
    except FileNotFoundError:
        return
    except PermissionError:
        chown = subprocess.run(
            ["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(scratch)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if chown.returncode == 0:
            try:
                shutil.rmtree(scratch)
                return
            except OSError as exc:
                logging.warning("Could not fully clean scratch %s after chown: %s", scratch, exc)
                return
        if scratch.parent == Path(tempfile.gettempdir()).resolve():
            cleanup_scratch(scratch)
            return
    except OSError as exc:
        logging.warning("Could not fully clean scratch %s: %s", scratch, exc)
        return
    logging.warning("Could not fully clean scratch %s due to permissions", scratch)


def _write_status(sample_dir: Path, **payload: Any) -> None:
    checkpoint = sample_dir / "checkpoint"
    checkpoint.mkdir(parents=True, exist_ok=True)
    data = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **payload,
    }
    (checkpoint / "status.json").write_text(
        json.dumps(data, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def normalize_harness_name(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in CLI_HARNESSES:
        raise ValueError(
            f"unsupported CLI harness {value!r}; expected one of {', '.join(CLI_HARNESSES)}"
        )
    return normalized


def _optional_config_value(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    cfg = GT_ROOT / "config.txt"
    if not cfg.is_file():
        return ""
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _load_api_key(env: dict[str, str], key_env: str) -> None:
    if not key_env:
        return
    if env.get(key_env):
        return
    try:
        env[key_env] = load_env_key(key_env)
    except Exception:
        # Let the underlying CLI emit its native auth error.  The manifest/logs
        # will still show the env key name, not the secret.
        return


def _host_submit_server_url(server: str) -> str:
    if sys.platform.startswith("linux"):
        return server.replace("host.docker.internal", "127.0.0.1")
    return server


def _rewrite_workspace_paths(workspace: Path) -> None:
    """Make the generated CyberGym workspace work for host-side CLI agents.

    The stock task template is written for containers mounted at /workspace.
    Codex/Claude/DSH CLI agents run from a host directory in this runner, so the
    generated submit.sh and description.txt must reference the concrete workspace path.
    """
    for path in (workspace / "description.txt", workspace / "submit.sh"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        text = text.replace("/workspace", str(workspace))
        path.write_text(text, encoding="utf-8")
    submit = workspace / "submit.sh"
    if submit.is_file():
        submit.chmod(0o755)


def _workspace_listing(workspace: Path) -> str:
    lines: list[str] = []
    for path in sorted(workspace.rglob("*")):
        rel = path.relative_to(workspace)
        if any(part in {".git", "__pycache__"} for part in rel.parts):
            continue
        if len(lines) >= 400:
            lines.append("... truncated ...")
            break
        kind = "d" if path.is_dir() else "f"
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        lines.append(f"{kind} {rel} {size}")
    return "\n".join(lines) + "\n"


def _should_start_codex_bridge(args: argparse.Namespace) -> bool:
    if not args.base_url:
        return False
    bridge = str(getattr(args, "codex_bridge", "auto") or "auto").strip().lower()
    if bridge in {"none", "off", "disabled"}:
        return False
    if bridge == "modelhub_crawl":
        return True
    if bridge not in {"", "auto"}:
        raise ValueError(
            "--codex-bridge must be one of auto, none, or modelhub_crawl; "
            f"got {bridge!r}"
        )
    clean_url = args.base_url.split("?", 1)[0].rstrip("/").lower()
    return (
        "modelhub" in clean_url
        or "/crawl/" in clean_url
        or clean_url.endswith("/messages")
        or clean_url.endswith("/chat/completions")
    )


def _bridge_target_url(base_url: str, api_version: str = "") -> str:
    target, separator, query = str(base_url or "").strip().partition("?")
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


def _start_codex_bridge(args: argparse.Namespace, env: dict[str, str], sample_dir: Path) -> tuple[subprocess.Popen | None, str]:
    if not _should_start_codex_bridge(args):
        return None, ""
    if not CODEX_BRIDGE.is_file():
        raise FileNotFoundError(f"missing Codex ModelHub bridge: {CODEX_BRIDGE}")
    runs = sample_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    bridge_dir = runs / "codex_modelhub_bridge"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    port_file = bridge_dir / "port"
    log_file = bridge_dir / "bridge.log"
    try:
        port_file.unlink()
    except FileNotFoundError:
        pass
    key_env = args.api_key_env or default_api_key_env(args.model)
    _load_api_key(env, key_env)
    cmd = [
        sys.executable,
        str(CODEX_BRIDGE),
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--port-file",
        str(port_file),
        "--target-url",
        _bridge_target_url(args.base_url, args.api_version),
        "--api-key-env",
        key_env,
        "--payload-format",
        str(getattr(args, "bridge_payload_format", "auto") or "auto"),
        "--max-tokens",
        str(args.max_output_tokens),
        "--timeout-seconds",
        str(args.timeout),
        "--log-file",
        str(log_file),
    ]
    if getattr(args, "bridge_caller", ""):
        cmd += ["--caller", args.bridge_caller]
    if getattr(args, "bridge_disable_proxy", False):
        cmd += ["--disable-proxy"]
    stream = log_file.open("a", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=GT_ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if port_file.is_file() and port_file.read_text(encoding="utf-8").strip():
            return proc, f"http://127.0.0.1:{port_file.read_text(encoding='utf-8').strip()}"
        if proc.poll() is not None:
            raise RuntimeError(f"Codex ModelHub bridge exited early; see {log_file}")
        time.sleep(0.05)
    raise RuntimeError(f"Codex ModelHub bridge did not become ready; see {log_file}")


def _codex_command(args: argparse.Namespace, workspace: Path, prompt: str, env: dict[str, str], sample_dir: Path) -> tuple[list[str], subprocess.Popen | None]:
    bridge_proc, bridge_base = _start_codex_bridge(args, env, sample_dir)
    provider_base = bridge_base or args.base_url
    command = [
        "codex",
        "exec",
        "--cd",
        str(workspace),
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "--strict-config",
    ]
    if provider_base:
        provider = "modelhub" if bridge_base else "custom"
        key_env = args.api_key_env or default_api_key_env(args.model)
        _load_api_key(env, key_env)
        command += [
            "-c",
            f"model_provider=\"{provider}\"",
            "-c",
            f"model_providers.{provider}.name=\"{provider}\"",
            "-c",
            f"model_providers.{provider}.base_url=\"{provider_base}\"",
            "-c",
            f"model_providers.{provider}.wire_api=\"responses\"",
            "-c",
            f"model_providers.{provider}.env_key=\"{key_env}\"",
        ]
    if args.model:
        command += ["-m", args.model]
    if args.codex_reasoning_effort:
        command += ["-c", f"model_reasoning_effort=\"{args.codex_reasoning_effort}\""]
    command.append(prompt)
    return command, bridge_proc


def _claude_model_name(model: str) -> str:
    if model == "sonnet-5":
        return "claude-sonnet-5"
    return model


def _write_claude_workspace_files(workspace: Path, run_cwd: Path, sample_id: str, arvo_id: str = "") -> None:
    description = workspace / "description.txt"
    if description.is_file():
        description.chmod(0o644)

def _prepare_claude_runtime(
    args: argparse.Namespace,
    workspace: Path,
    sample_id: str,
    scratch: Path,
    env: dict[str, str],
    checkpoint: Path,
) -> dict[str, Any]:
    claude_home = scratch / "claude_home"
    claude_home.mkdir(parents=True, exist_ok=True)
    run_cwd = workspace
    _write_claude_workspace_files(workspace, run_cwd, sample_id, str(getattr(args, "arvo_id", "") or ""))
    env["HOME"] = str(claude_home)
    config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or claude_home / ".claude")
    config_dir.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["BENCHMARK_WORKSPACE"] = str(workspace)
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    if token:
        env.setdefault("ANTHROPIC_AUTH_TOKEN", token)
        env.setdefault("ANTHROPIC_API_KEY", token)
    return {
        "cwd": str(run_cwd),
        "home": str(claude_home),
        "config_dir": str(config_dir),
        "stdout_jsonl": "checkpoint/claude_stdout.jsonl",
        "workspace_instruction_files": [],
    }


def _claude_command(args: argparse.Namespace, workspace: Path, prompt: str, env: dict[str, str]) -> tuple[list[str], None]:
    key_env = args.api_key_env or "ANTHROPIC_AUTH_TOKEN"
    _load_api_key(env, key_env)
    if key_env == "ANTHROPIC_AUTH_TOKEN" and not env.get("ANTHROPIC_AUTH_TOKEN"):
        _load_api_key(env, "ANTHROPIC_API_KEY")
    if key_env != "ANTHROPIC_AUTH_TOKEN" and env.get(key_env) and not env.get("ANTHROPIC_AUTH_TOKEN"):
        env["ANTHROPIC_AUTH_TOKEN"] = env[key_env]
    if env.get("ANTHROPIC_AUTH_TOKEN") and not env.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = env["ANTHROPIC_AUTH_TOKEN"]
    if env.get("ANTHROPIC_API_KEY") and not env.get("ANTHROPIC_AUTH_TOKEN"):
        env["ANTHROPIC_AUTH_TOKEN"] = env["ANTHROPIC_API_KEY"]
    if args.base_url:
        env["ANTHROPIC_BASE_URL"] = args.base_url
    else:
        base = _optional_config_value("ANTHROPIC_BASE_URL")
        if base:
            env["ANTHROPIC_BASE_URL"] = base
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    env.setdefault("CLAUDE_CODE_ATTRIBUTION_HEADER", "0")
    command = [
        "claude",
        "-p",
        prompt,
        "--tools",
        "Bash,Read,Write,Edit,Glob,Grep",
        "--allowedTools",
        "Bash Read Write Edit Glob Grep",
        "--add-dir",
        str(workspace),
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--effort",
        "max",
        "--model",
        _claude_model_name(args.model),
    ]
    return command, None


def _agent_command(args: argparse.Namespace, workspace: Path, prompt: str, env: dict[str, str], sample_dir: Path) -> tuple[list[str], subprocess.Popen | None]:
    harness = normalize_harness_name(args.harness)
    if harness == "codex":
        return _codex_command(args, workspace, prompt, env, sample_dir)
    if harness == "claude":
        return _claude_command(args, workspace, prompt, env)
    raise ValueError(f"unsupported CLI harness: {args.harness}")


def _manifest_api_key_env(args: argparse.Namespace) -> str:
    if args.api_key_env:
        return args.api_key_env
    if normalize_harness_name(args.harness) == "claude":
        return "ANTHROPIC_AUTH_TOKEN"
    return default_api_key_env(args.model)


def _workspace_relative(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def _analysis_candidate_error(candidate_path: Path, sample_dir: Path) -> str | None:
    try:
        raw = candidate_path.read_text(encoding="utf-8")
        artifact = json.loads(raw)
    except OSError as exc:
        return f"read_error:{type(exc).__name__}: {exc}"
    except (TypeError, json.JSONDecodeError) as exc:
        return f"json_error:{exc}"
    if (
        not isinstance(artifact, dict)
        or not isinstance(artifact.get("sample_id"), str)
        or not artifact["sample_id"].strip()
        or not isinstance(artifact.get("fine_trace"), list)
        or not isinstance(artifact.get("vuln_logic"), dict)
    ):
        return "schema_error: expected JSON object with sample_id, fine_trace, and vuln_logic"
    if artifact.get("sample_id") != sample_dir.name:
        return f"sample_id_mismatch: expected {sample_dir.name!r}, got {artifact.get('sample_id')!r}"
    quality_error = validate_analysis_artifact_quality(raw)
    if quality_error is not None:
        return f"quality_error: {quality_error}"
    return None


def _copy_latest_analysis(workspace: Path, sample_dir: Path) -> tuple[bool, str, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    for candidate in _analysis_candidates(workspace):
        if not candidate.is_file():
            continue
        rel = _workspace_relative(candidate, workspace)
        error = _analysis_candidate_error(candidate, sample_dir)
        if error is not None:
            diagnostics.append({"path": rel, "accepted": False, "error": error})
            continue
        if persist_analysis_artifact(candidate, sample_dir):
            diagnostics.append({"path": rel, "accepted": True})
            return True, rel, diagnostics
        diagnostics.append(
            {
                "path": rel,
                "accepted": False,
                "error": "persist_analysis_artifact returned false after validation",
            }
        )
    existing = (sample_dir / "analysis.json").is_file()
    return existing, "existing" if existing else "none", diagnostics


def _analysis_candidates(workspace: Path) -> tuple[Path, ...]:
    return (
        workspace / ".latest_analysis.json",
        workspace / "analysis.json",
        workspace / ".final_analysis.json",
    )


def _select_valid_fallback_analysis(workspace: Path, sample_dir: Path) -> Path | None:
    for candidate in _analysis_candidates(workspace):
        if candidate.is_file() and _analysis_candidate_error(candidate, sample_dir) is None:
            return candidate
    return None


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def _looks_like_analysis_artifact(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("sample_id"), str)
        and isinstance(value.get("fine_trace"), list)
        and isinstance(value.get("vuln_logic"), dict)
    )


def _json_objects_from_text(text: str) -> list[tuple[str, dict[str, Any]]]:
    stripped = text.strip()
    candidates: list[tuple[str, str]] = [("whole_message", stripped)]
    candidates.extend(
        (f"fenced_json_{index}", match.group(1).strip())
        for index, match in enumerate(_JSON_FENCE_RE.finditer(stripped), 1)
    )

    decoder = json.JSONDecoder()
    for index, match in enumerate(re.finditer(r"\{", stripped), 1):
        try:
            _value, end = decoder.raw_decode(stripped[match.start() :])
        except json.JSONDecodeError:
            continue
        candidates.append(
            (
                f"embedded_json_{index}",
                stripped[match.start() : match.start() + end],
            )
        )

    objects: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for source, candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        objects.append((source, value))
    return objects


def _write_checkpoint(sample_dir: Path, workspace: Path, prompt: str, task_payload: dict[str, Any], log_path: Path) -> None:
    checkpoint = sample_dir / "checkpoint"
    checkpoint.mkdir(parents=True, exist_ok=True)
    (checkpoint / "prompt.txt").write_text(prompt, encoding="utf-8")
    (checkpoint / "task.json").write_text(json.dumps(task_payload, indent=2, default=str) + "\n", encoding="utf-8")
    (checkpoint / "workspace_listing.txt").write_text(_workspace_listing(workspace), encoding="utf-8")
    if (workspace / "description.txt").is_file():
        shutil.copy2(workspace / "description.txt", checkpoint / "description.txt")
    if log_path.is_file():
        shutil.copy2(log_path, checkpoint / "agent.log")


def _shorten_text(value: str, limit: int = 2000) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _format_claude_tool_use(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "unknown")
    tool_input = item.get("input") or {}
    if not isinstance(tool_input, dict):
        return f"TOOL {name}: {_shorten_text(str(tool_input))}"
    if name == "Bash":
        value = tool_input.get("command") or ""
    elif name == "Read":
        value = tool_input.get("file_path") or ""
    elif name in {"Grep", "Glob"}:
        value = tool_input.get("pattern") or tool_input.get("path") or ""
    elif name in {"Write", "Edit"}:
        value = tool_input.get("file_path") or ""
    else:
        value = json.dumps(tool_input, ensure_ascii=False, default=str)
    return f"TOOL {name}: {_shorten_text(str(value))}"


def _write_claude_transcript(checkpoint: Path) -> bool:
    stdout_jsonl = checkpoint / "claude_stdout.jsonl"
    if not stdout_jsonl.is_file():
        return False
    lines: list[str] = []
    for raw in stdout_jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            lines.append(f"RAW: {_shorten_text(raw)}")
            continue
        event_type = payload.get("type")
        if event_type == "system" and payload.get("subtype") == "init":
            tools = ",".join(payload.get("tools") or [])
            lines.append(
                "SYSTEM init "
                f"cwd={payload.get('cwd')} model={payload.get('model')} "
                f"permission={payload.get('permissionMode')} tools={tools}"
            )
        elif event_type == "assistant":
            message = payload.get("message") or {}
            for item in message.get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        lines.append(f"ASSISTANT: {_shorten_text(text)}")
                elif item.get("type") == "tool_use":
                    lines.append(_format_claude_tool_use(item))
        elif event_type == "user":
            message = payload.get("message") or {}
            for item in message.get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_result":
                    content = item.get("content")
                    if isinstance(content, str) and content.strip():
                        lines.append(f"TOOL_RESULT: {_shorten_text(content)}")
                elif item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        lines.append(f"USER: {_shorten_text(text)}")
        elif event_type == "result":
            result = payload.get("result")
            if isinstance(result, str) and result.strip():
                lines.append(f"RESULT: {_shorten_text(result)}")
            else:
                lines.append(
                    "RESULT "
                    f"subtype={payload.get('subtype')} "
                    f"stop={payload.get('stop_reason') or payload.get('terminal_reason')}"
                )
    (checkpoint / "claude_transcript.txt").write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
    )
    return True


def _candidate_artifact_name(path: Path) -> bool:
    name = path.name.lower()
    stem = path.stem.lower()
    if name in {"analysis.json", ".latest_analysis.json", ".final_analysis.json"}:
        return True
    prefixes = ("poc", "crash", "repro", "candidate", "payload", "testcase")
    suffixes = (".poc", ".crash", ".repro", ".input", ".bin", ".dat", ".ps")
    return stem.startswith(prefixes) or name.endswith(suffixes)


def _copy_workspace_artifacts(
    workspace: Path,
    checkpoint: Path,
    *,
    agent_started_wall: float | None,
) -> list[dict[str, Any]]:
    target = checkpoint / "workspace_artifacts"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    excluded_dirs = {".benchmark_runner", ".git", "__pycache__"}
    excluded_top_files = {
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "build.sh",
        "description.txt",
        "submit.sh",
    }
    max_bytes = 8 * 1024 * 1024
    max_files = 200
    copied: list[dict[str, Any]] = []
    skipped = 0
    for path in sorted(workspace.rglob("*")):
        try:
            rel = path.relative_to(workspace)
        except ValueError:
            continue
        if path.is_dir() or path.is_symlink():
            continue
        if any(part in excluded_dirs for part in rel.parts):
            continue
        if len(rel.parts) == 1 and rel.name in excluded_top_files:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        is_candidate = _candidate_artifact_name(rel)
        modified_by_agent = (
            agent_started_wall is not None and stat.st_mtime >= agent_started_wall - 2
        )
        under_source_tree = bool(rel.parts and rel.parts[0] == "repo-vul")
        if under_source_tree and not (is_candidate and modified_by_agent):
            continue
        if not under_source_tree and not (is_candidate or modified_by_agent):
            continue
        entry: dict[str, Any] = {"path": str(rel), "size": stat.st_size}
        if len(copied) >= max_files:
            skipped += 1
            continue
        if stat.st_size > max_bytes:
            entry["copied"] = False
            entry["reason"] = "too_large"
            copied.append(entry)
            continue
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, dest)
        except OSError as exc:
            entry["copied"] = False
            entry["reason"] = f"copy_failed:{type(exc).__name__}"
            entry["error"] = str(exc)
            copied.append(entry)
            continue
        entry["copied"] = True
        entry["checkpoint_path"] = str(Path("checkpoint") / "workspace_artifacts" / rel)
        copied.append(entry)
    manifest = {
        "workspace": str(workspace),
        "max_bytes_per_file": max_bytes,
        "max_files": max_files,
        "skipped_after_limit": skipped,
        "artifacts": copied,
    }
    (checkpoint / "workspace_artifacts_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return copied


def _record_claude_stream_event(line: str, telemetry: dict[str, Any]) -> None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        telemetry["non_json_lines"] = int(telemetry.get("non_json_lines", 0)) + 1
        return
    event_type = str(payload.get("type") or "unknown")
    event_counts = telemetry.setdefault("event_counts", {})
    event_counts[event_type] = int(event_counts.get(event_type, 0)) + 1
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        session_ids = telemetry.setdefault("session_ids", [])
        if session_id not in session_ids:
            session_ids.append(session_id)
    if event_type == "assistant":
        message = payload.get("message") or {}
        for item in message.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                name = str(item.get("name") or "unknown")
                telemetry["tool_calls"] = int(telemetry.get("tool_calls", 0)) + 1
                tool_counts = telemetry.setdefault("tool_counts", {})
                tool_counts[name] = int(tool_counts.get(name, 0)) + 1
                tool_input = item.get("input") or {}
                if isinstance(tool_input, dict) and "submit.sh" in str(tool_input.get("command") or ""):
                    telemetry["submission_command_calls"] = int(
                        telemetry.get("submission_command_calls", 0)
                    ) + 1
            elif item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    telemetry["last_assistant_text"] = text
    elif event_type == "result":
        result = payload.get("result")
        if isinstance(result, str):
            telemetry["final_message"] = result
        telemetry["terminal_reason"] = payload.get("terminal_reason")
        telemetry["stop_reason"] = payload.get("stop_reason")
        telemetry["duration_ms"] = payload.get("duration_ms")
        telemetry["total_cost_usd"] = payload.get("total_cost_usd")


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        proc.wait(timeout=5)


def _run_claude_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    log_stream: Any,
    checkpoint: Path,
) -> tuple[int, bool, dict[str, Any]]:
    stdout_jsonl = checkpoint / "claude_stdout.jsonl"
    telemetry: dict[str, Any] = {
        "backend": "claude",
        "stdout": "claude_stdout.jsonl",
        "jsonl_lines": 0,
        "event_counts": {},
        "tool_calls": 0,
        "tool_counts": {},
        "session_ids": [],
    }
    started = time.monotonic()
    timed_out = False
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None
    selector.register(proc.stdout, selectors.EVENT_READ)
    with stdout_jsonl.open("w", encoding="utf-8") as jsonl:
        while True:
            remaining = None
            if timeout:
                remaining = max(0.0, started + timeout - time.monotonic())
                if remaining <= 0:
                    timed_out = True
                    telemetry["kill_reason"] = f"timeout:{timeout}"
                    _terminate_process(proc)
                    if proc.stdout is not None:
                        rest = proc.stdout.read()
                        if rest:
                            for line in rest.splitlines(keepends=True):
                                log_stream.write(line)
                                jsonl.write(line)
                                telemetry["jsonl_lines"] = int(telemetry.get("jsonl_lines", 0)) + 1
                                _record_claude_stream_event(line, telemetry)
                            log_stream.flush()
                            jsonl.flush()
                    break
            events = selector.select(timeout=min(1.0, remaining) if remaining is not None else 1.0)
            for key, _ in events:
                line = key.fileobj.readline()
                if line == "":
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:
                        pass
                    continue
                log_stream.write(line)
                log_stream.flush()
                jsonl.write(line)
                jsonl.flush()
                telemetry["jsonl_lines"] = int(telemetry.get("jsonl_lines", 0)) + 1
                _record_claude_stream_event(line, telemetry)
            if proc.poll() is not None:
                if proc.stdout is not None:
                    rest = proc.stdout.read()
                    if rest:
                        for line in rest.splitlines(keepends=True):
                            log_stream.write(line)
                            jsonl.write(line)
                            telemetry["jsonl_lines"] = int(telemetry.get("jsonl_lines", 0)) + 1
                            _record_claude_stream_event(line, telemetry)
                        log_stream.flush()
                        jsonl.flush()
                break
    try:
        selector.close()
    except Exception:
        pass
    returncode = 124 if timed_out else proc.returncode if proc.returncode is not None else 124
    telemetry["returncode"] = returncode
    telemetry["timed_out"] = timed_out
    telemetry["seconds"] = round(time.monotonic() - started, 3)
    final_message = telemetry.get("final_message") or telemetry.get("last_assistant_text")
    if isinstance(final_message, str) and final_message.strip():
        (checkpoint / "final_message.txt").write_text(final_message, encoding="utf-8")
    _write_claude_transcript(checkpoint)
    (checkpoint / "claudecli_invocations.json").write_text(
        json.dumps({"invocations": [telemetry]}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return returncode, timed_out, telemetry


def _copy_claude_runtime_artifacts(runtime: dict[str, Any] | None, checkpoint: Path) -> None:
    if not runtime:
        return
    destinations: set[Path] = set()
    for root_name in ("home", "config_dir"):
        value = runtime.get(root_name)
        if not isinstance(value, str) or not value:
            continue
        root = Path(value)
        project_dir = root / ".claude" / "projects" if root_name == "home" else root / "projects"
        if project_dir.is_dir():
            destinations.add(project_dir)
    if not destinations:
        return
    target = checkpoint / "claude_home" / ".claude" / "projects"
    errors: list[dict[str, str]] = []
    for project_dir in destinations:
        try:
            shutil.copytree(project_dir, target, dirs_exist_ok=True)
        except OSError as exc:
            errors.append(
                {
                    "source": str(project_dir),
                    "target": str(target),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    if errors:
        (checkpoint / "claude_runtime_artifacts_errors.json").write_text(
            json.dumps(errors, indent=2) + "\n",
            encoding="utf-8",
        )


def _persist_final_analysis_from_text(
    text: str | None,
    workspace: Path,
    sample_id: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "extracted": False,
        "path": None,
        "source": None,
    }
    if not text:
        result["error"] = "empty_final_message"
        return result
    objects = [
        (source, artifact)
        for source, artifact in _json_objects_from_text(text)
        if _looks_like_analysis_artifact(artifact)
    ]
    if not objects:
        result["error"] = "no_analysis_json_object"
        return result
    source, artifact = next(
        (
            (candidate_source, candidate)
            for candidate_source, candidate in objects
            if candidate.get("sample_id") == sample_id
        ),
        objects[0],
    )
    output = workspace / ".final_analysis.json"
    output.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    result.update({"extracted": True, "path": ".final_analysis.json", "source": source})
    if artifact.get("sample_id") != sample_id:
        result["warning"] = (
            f"sample_id_mismatch: expected {sample_id!r}, got {artifact.get('sample_id')!r}"
        )
    return result


def run_once(args: argparse.Namespace, sample_id: str, task_id: str, results_dir: Path) -> str:
    harness = normalize_harness_name(args.harness)
    sample_dir = results_dir / sample_id
    scratch = _make_scratch(sample_id, harness)
    workspace = scratch / "workspace"
    workspace.mkdir(parents=True)
    checkpoint = sample_dir / "checkpoint"
    if checkpoint.exists():
        shutil.rmtree(checkpoint)
    checkpoint.mkdir(parents=True, exist_ok=True)
    _write_status(
        sample_dir,
        phase="prepare_workspace",
        harness=harness,
        sample_id=sample_id,
        scratch=str(scratch),
        workspace=str(workspace),
    )
    run_dir = sample_dir / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{harness}_{int(time.time())}.log"
    bridge_proc: subprocess.Popen | None = None
    started = time.monotonic()
    timed_out = False
    returncode: int | None = None
    cli_runtime: dict[str, Any] | None = None
    cli_telemetry: dict[str, Any] | None = None
    final_analysis_extraction: dict[str, Any] | None = None
    analysis_candidates: list[dict[str, Any]] = []
    agent_started_wall: float | None = None
    workspace_artifacts: list[dict[str, Any]] = []
    prompt = ""
    command: list[str] | None = None
    task_payload: dict[str, Any] = {
        "sample_id": sample_id,
        "task_id": task_id,
        "harness": harness,
        "model": args.model,
        "scratch": str(scratch),
    }
    try:
        task = generate_arvo_task(
            TaskConfig(
                task_id=task_id,
                out_dir=workspace,
                data_dir=GT_ROOT / "external" / "cybergym_data_subset" / "data",
                server=_host_submit_server_url(args.server),
                difficulty=TaskDifficulty(args.difficulty),
            )
        )
        _write_status(
            sample_dir,
            phase="workspace_generated",
            harness=harness,
            sample_id=sample_id,
            scratch=str(scratch),
            workspace=str(workspace),
            agent_id=task.agent_id,
        )
        _rewrite_workspace_paths(workspace)
        install_submit_candidate_guard(workspace / "submit.sh")
        env = os.environ.copy()
        if harness == "claude":
            cli_runtime = _prepare_claude_runtime(
                args, workspace, sample_id, scratch, env, checkpoint
            )
        adapter_metadata = run_workspace_installer(
            args.workspace_installer,
            harness=harness,
            workspace=workspace,
            sample_id=sample_id,
            scratch=scratch,
            env=env,
        )
        prompt = render_prompt(
            args.prompt_file, sample_id=sample_id, workspace=workspace
        )
        command, bridge_proc = _agent_command(args, workspace, prompt, env, sample_dir)
        task_payload = {
            "task_id": task.task_id,
            "agent_id": task.agent_id,
            "checksum": task.checksum,
            "server": task.server,
            "difficulty": str(task.difficulty),
            "harness": harness,
            "model": args.model,
            "workspace_adapter": adapter_metadata,
            "cli_runtime": cli_runtime,
        }
        _write_checkpoint(sample_dir, workspace, prompt, task_payload, log_path)
        _write_status(
            sample_dir,
            phase="agent_running",
            harness=harness,
            sample_id=sample_id,
            scratch=str(scratch),
            workspace=str(workspace),
            agent_id=task.agent_id,
            checkpoint=str(checkpoint),
            log=str(log_path),
        )
        with log_path.open("w", encoding="utf-8") as stream:
            stream.write("COMMAND " + json.dumps(command, ensure_ascii=False) + "\n")
            stream.flush()
            agent_started_wall = time.time()
            if harness == "claude":
                returncode, timed_out, cli_telemetry = _run_claude_command(
                    command,
                    cwd=Path(str(cli_runtime["cwd"])) if cli_runtime else workspace,
                    env=env,
                    timeout=args.timeout,
                    log_stream=stream,
                    checkpoint=checkpoint,
                )
                final_analysis_extraction = _persist_final_analysis_from_text(
                    cli_telemetry.get("final_message") if cli_telemetry else None,
                    workspace,
                    sample_id,
                )
            else:
                try:
                    completed = subprocess.run(
                        command,
                        cwd=workspace,
                        env=env,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=args.timeout,
                    )
                    returncode = completed.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    returncode = 124
        _write_status(
            sample_dir,
            phase="collecting_results",
            harness=harness,
            sample_id=sample_id,
            scratch=str(scratch),
            workspace=str(workspace),
            agent_id=task.agent_id,
            returncode=returncode,
            timed_out=timed_out,
            checkpoint=str(checkpoint),
        )
        db_path = args.server_root / "poc.db"
        success_info = check_success(db_path, task.agent_id) if db_path.exists() else {"ok": False, "error": "db not found", "submission_attempts": [], "success": False}
        fallback_analysis = _select_valid_fallback_analysis(workspace, sample_dir)
        persisted_attempts = persist_submission_attempts(
            sample_dir,
            task.agent_id,
            success_info.get("submission_attempts") or [],
            fallback_analysis,
            args.server_root,
        )
        poc_deduplication, deduplicated_pocs = deduplicate_submission_attempts(persisted_attempts)
        analysis_source = "none"
        valid_attempts = [attempt for attempt in persisted_attempts if attempt.get("analysis_valid")]
        if valid_attempts:
            latest = valid_attempts[-1]
            candidate_path = (
                sample_dir / str(latest["analysis_path"])
                if latest.get("analysis_path")
                else sample_dir / str(latest["result_path"]) / "analysis.json"
            )
            if candidate_path.is_file() and persist_analysis_artifact(candidate_path, sample_dir):
                analysis_source = "last_valid_poc_submission"
        if analysis_source == "none":
            produced, source, analysis_candidates = _copy_latest_analysis(workspace, sample_dir)
            if produced:
                analysis_source = source
        (checkpoint / "analysis_candidates.json").write_text(
            json.dumps(
                {
                    "candidates": analysis_candidates,
                    "final_message_extraction": final_analysis_extraction,
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        analysis_produced = (sample_dir / "analysis.json").is_file()
        if success_info.get("success"):
            status = "success"
            stop_reason = "successful_submission"
        elif timed_out:
            status = "timeout"
            stop_reason = f"timeout:{args.timeout}"
        elif analysis_produced or persisted_attempts:
            status = "agent_finished"
            stop_reason = "cli_exit"
        else:
            status = "incomplete"
            stop_reason = "no_analysis_or_submission"
        _write_checkpoint(sample_dir, workspace, prompt, task_payload, log_path)
        if harness == "claude":
            _copy_claude_runtime_artifacts(cli_runtime, checkpoint)
            workspace_artifacts = _copy_workspace_artifacts(
                workspace,
                checkpoint,
                agent_started_wall=agent_started_wall,
            )
        manifest = {
            "evaluation_protocol": "poc_analysis_artifact_per_submission_v3_cli",
            "arvo_id": args.arvo_id,
            "task_id": task_id,
            "sample_id": sample_id,
            "cybergym_agent_id": task.agent_id,
            "model": args.model,
            "harness": harness,
            "api_key_env": _manifest_api_key_env(args),
            "base_url_configured": bool(args.base_url),
            "codex_bridge": (args.codex_bridge if harness == "codex" else ""),
            "bridge_payload_format": (args.bridge_payload_format if harness == "codex" else ""),
            "max_iter": args.max_iter,
            "timeout": args.timeout,
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "stop_reason": stop_reason,
            "seconds": round(time.monotonic() - started, 3),
            "workspace_adapter": adapter_metadata,
            "cli_runtime": cli_runtime,
            "cli_telemetry": cli_telemetry,
            "poc_generation": success_info,
            "num_submission_attempts": len(persisted_attempts),
            "submission_attempts": persisted_attempts,
            "poc_deduplication": poc_deduplication,
            "deduplicated_pocs": deduplicated_pocs,
            "analysis": {
                "produced": analysis_produced,
                "source": analysis_source,
                "path": "analysis.json",
                "format": "JSON object with sample_id, fine_trace, and vuln_logic",
                "candidates": analysis_candidates,
                "final_message_extraction": final_analysis_extraction,
            },
            "checkpoint": {
                "dir": "checkpoint/",
                "phase": "cli_terminal",
                "contains_workspace_listing": True,
                "contains_claude_stdout_jsonl": (checkpoint / "claude_stdout.jsonl").is_file(),
                "contains_claude_transcript": (checkpoint / "claude_transcript.txt").is_file(),
                "workspace_artifacts": workspace_artifacts,
                "note": "The extracted workspace is not persisted; checkpoint stores prompt, README, task metadata, listing, agent log, and selected generated workspace artifacts.",
            },
        }
        (sample_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        _write_status(
            sample_dir,
            phase="finished",
            harness=harness,
            sample_id=sample_id,
            status=status,
            stop_reason=stop_reason,
            returncode=returncode,
            timed_out=timed_out,
            submission_attempts=len(persisted_attempts),
            analysis_produced=analysis_produced,
        )
        print(json.dumps(manifest, indent=2, default=str), flush=True)
        return status
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        try:
            if prompt or task_payload:
                _write_checkpoint(sample_dir, workspace, prompt, task_payload, log_path)
                if harness == "claude":
                    _write_claude_transcript(checkpoint)
                    _copy_claude_runtime_artifacts(cli_runtime, checkpoint)
                    workspace_artifacts = _copy_workspace_artifacts(
                        workspace,
                        checkpoint,
                        agent_started_wall=agent_started_wall,
                    )
        except Exception as checkpoint_exc:  # noqa: BLE001
            logging.warning("Could not write failure checkpoint for %s: %s", sample_id, checkpoint_exc)
        manifest = write_failure_artifact(
            sample_dir,
            sample_id=sample_id,
            harness=harness,
            model=args.model,
            framework=(
                "reward_framework"
                if os.getenv("REWARD_FRAMEWORK_RUN_ID")
                else "poc_generation"
            ),
            evaluation_protocol="poc_analysis_artifact_per_submission_v3_cli",
            status="error",
            stop_reason="runner_exception",
            error=error,
            returncode=returncode,
            timed_out=timed_out,
            seconds=round(time.monotonic() - started, 3),
            command=command,
            log_path=log_path if log_path.is_file() else None,
            extra={
                "arvo_id": args.arvo_id,
                "task_id": task_id,
                "api_key_env": _manifest_api_key_env(args),
                "base_url_configured": bool(args.base_url),
                "max_iter": args.max_iter,
                "timeout": args.timeout,
                "workspace_adapter": None,
                "cli_runtime": cli_runtime,
                "cli_telemetry": cli_telemetry,
                "analysis": {
                    "candidates": analysis_candidates,
                    "final_message_extraction": final_analysis_extraction,
                },
                "cli_checkpoint": {
                    "dir": "checkpoint/",
                    "phase": "runner_exception",
                    "contains_workspace_listing": (checkpoint / "workspace_listing.txt").is_file(),
                    "contains_claude_stdout_jsonl": (checkpoint / "claude_stdout.jsonl").is_file(),
                    "contains_claude_transcript": (checkpoint / "claude_transcript.txt").is_file(),
                    "workspace_artifacts": workspace_artifacts,
                    "note": "Failure manifest written by harness_runtime before cleanup.",
                },
            },
            overwrite_manifest=True,
        )
        _write_status(
            sample_dir,
            phase="failed",
            harness=harness,
            sample_id=sample_id,
            status="error",
            error=error,
            returncode=returncode,
            timed_out=timed_out,
        )
        print(json.dumps(manifest, indent=2, default=str), flush=True)
        return "error"
    finally:
        if bridge_proc is not None:
            bridge_proc.terminate()
            try:
                bridge_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge_proc.kill()
                bridge_proc.wait(timeout=5)
        _cleanup_cli_scratch(scratch)



def _copy_local_submissions(workspace: Path, sample_dir: Path) -> None:
    src = workspace / ".submissions"
    dst = sample_dir / "submissions"
    if dst.exists():
        shutil.rmtree(dst)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)


def run_local_once(args: argparse.Namespace, sample_id: str, results_dir: Path) -> str:
    harness = normalize_harness_name(args.harness)
    gt_sample_dir = GT_ROOT / "gt_results" / sample_id
    sample_dir = results_dir / sample_id
    clear_local_previous_result(sample_dir)
    sample_dir.mkdir(parents=True, exist_ok=True)
    scratch = _make_scratch(sample_id, harness)
    checkpoint = sample_dir / "checkpoint"
    checkpoint.mkdir(parents=True, exist_ok=True)
    run_dir = sample_dir / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{harness}_{int(time.time())}.log"
    bridge_proc: subprocess.Popen | None = None
    bridge: LocalExecutionBridge | None = None
    started = time.monotonic()
    timed_out = False
    returncode: int | None = None
    cli_runtime: dict[str, Any] | None = None
    cli_telemetry: dict[str, Any] | None = None
    final_analysis_extraction: dict[str, Any] | None = None
    analysis_candidates: list[dict[str, Any]] = []
    workspace_artifacts: list[dict[str, Any]] = []
    agent_started_wall: float | None = None
    prompt = ""
    command: list[str] | None = None
    adapter_metadata: dict[str, Any] | None = None
    runtime_readiness: dict[str, Any] | None = None
    task_payload: dict[str, Any] = {
        "sample_id": sample_id,
        "task_id": sample_id,
        "harness": harness,
        "model": args.model,
        "scratch": str(scratch),
        "local_runtime": True,
    }
    try:
        runtime_readiness = check_local_runtime_readiness(gt_sample_dir)
        workspace, inner_command, repro = prepare_local_workspace(sample_id, scratch)
        env = os.environ.copy()
        env["BENCHMARK_WORKSPACE"] = str(workspace)
        if harness == "claude":
            cli_runtime = _prepare_claude_runtime(
                args, workspace, sample_id, scratch, env, checkpoint
            )
        bridge = LocalExecutionBridge(workspace, inner_command, repro)
        bridge.start()
        write_local_submit_sh(workspace, bridge.url, bridge.token)
        adapter_metadata = run_workspace_installer(
            args.workspace_installer,
            harness=harness,
            workspace=workspace,
            sample_id=sample_id,
            scratch=scratch,
            env=env,
        )
        prompt = render_prompt(args.prompt_file, sample_id=sample_id, workspace=workspace)
        command, bridge_proc = _agent_command(args, workspace, prompt, env, sample_dir)
        task_payload.update(
            {
                "workspace": str(workspace),
                "harness": harness,
                "model": args.model,
                "workspace_adapter": adapter_metadata,
                "cli_runtime": cli_runtime,
                "runtime_readiness": runtime_readiness,
                "reproduction_inner_command": inner_command,
                "runtime": repro,
            }
        )
        _write_checkpoint(sample_dir, workspace, prompt, task_payload, log_path)
        _write_status(
            sample_dir,
            phase="agent_running",
            harness=harness,
            sample_id=sample_id,
            scratch=str(scratch),
            workspace=str(workspace),
            checkpoint=str(checkpoint),
            log=str(log_path),
        )
        with log_path.open("w", encoding="utf-8") as stream:
            stream.write("COMMAND " + json.dumps(command, ensure_ascii=False) + "\n")
            stream.flush()
            agent_started_wall = time.time()
            if harness == "claude":
                returncode, timed_out, cli_telemetry = _run_claude_command(
                    command,
                    cwd=Path(str(cli_runtime["cwd"])) if cli_runtime else workspace,
                    env=env,
                    timeout=args.timeout,
                    log_stream=stream,
                    checkpoint=checkpoint,
                )
                final_analysis_extraction = _persist_final_analysis_from_text(
                    cli_telemetry.get("final_message") if cli_telemetry else None,
                    workspace,
                    sample_id,
                )
            else:
                try:
                    completed = subprocess.run(
                        command,
                        cwd=workspace,
                        env=env,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=args.timeout,
                    )
                    returncode = completed.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    returncode = 124
        _write_status(
            sample_dir,
            phase="collecting_results",
            harness=harness,
            sample_id=sample_id,
            scratch=str(scratch),
            workspace=str(workspace),
            returncode=returncode,
            timed_out=timed_out,
            checkpoint=str(checkpoint),
        )
        submissions = validate_submissions_on_host(gt_sample_dir, workspace, inner_command)
        _copy_local_submissions(workspace, sample_dir)
        poc_deduplication, deduplicated_pocs = deduplicate_submission_attempts(submissions)
        analysis_source = "none"
        valid_attempts = [attempt for attempt in submissions if attempt.get("analysis_valid")]
        if valid_attempts:
            latest = valid_attempts[-1]
            candidate_path = workspace / ".submissions" / latest["attempt_id"] / "analysis.json"
            if candidate_path.is_file() and persist_analysis_artifact(candidate_path, sample_dir):
                analysis_source = "last_valid_poc_submission"
        if analysis_source == "none":
            produced, source, analysis_candidates = _copy_latest_analysis(workspace, sample_dir)
            if produced:
                analysis_source = source
        (checkpoint / "analysis_candidates.json").write_text(
            json.dumps(
                {
                    "candidates": analysis_candidates,
                    "final_message_extraction": final_analysis_extraction,
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        analysis_produced = (sample_dir / "analysis.json").is_file()
        triggered = any(item.get("triggered") is True for item in submissions)
        if triggered:
            status = "success"
            stop_reason = "successful_submission"
        elif timed_out:
            status = "timeout"
            stop_reason = f"timeout:{args.timeout}"
        elif analysis_produced or submissions:
            status = "agent_finished"
            stop_reason = "cli_exit"
        else:
            status = "incomplete"
            stop_reason = "no_analysis_or_submission"
        _write_checkpoint(sample_dir, workspace, prompt, task_payload, log_path)
        if harness == "claude":
            _copy_claude_runtime_artifacts(cli_runtime, checkpoint)
            workspace_artifacts = _copy_workspace_artifacts(
                workspace,
                checkpoint,
                agent_started_wall=agent_started_wall,
            )
        manifest = {
            "evaluation_protocol": "poc_analysis_artifact_per_submission_v3_cli_local",
            "sample_id": sample_id,
            "model": args.model,
            "harness": harness,
            "api_key_env": _manifest_api_key_env(args),
            "base_url_configured": bool(args.base_url),
            "codex_bridge": (args.codex_bridge if harness == "codex" else ""),
            "bridge_payload_format": (args.bridge_payload_format if harness == "codex" else ""),
            "max_iter": args.max_iter,
            "timeout": args.timeout,
            "runtime_readiness": runtime_readiness,
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "stop_reason": stop_reason,
            "seconds": round(time.monotonic() - started, 3),
            "workspace_adapter": adapter_metadata,
            "cli_runtime": cli_runtime,
            "cli_telemetry": cli_telemetry,
            "num_submission_attempts": len(submissions),
            "submission_attempts": submissions,
            "poc_deduplication": poc_deduplication,
            "deduplicated_pocs": deduplicated_pocs,
            "analysis": {
                "produced": analysis_produced,
                "source": analysis_source,
                "path": "analysis.json",
                "format": "JSON object with sample_id, fine_trace, and vuln_logic",
                "candidates": analysis_candidates,
                "final_message_extraction": final_analysis_extraction,
            },
            "checkpoint": {
                "dir": "checkpoint/",
                "phase": "cli_terminal",
                "contains_workspace_listing": True,
                "contains_claude_stdout_jsonl": (checkpoint / "claude_stdout.jsonl").is_file(),
                "contains_claude_transcript": (checkpoint / "claude_transcript.txt").is_file(),
                "workspace_artifacts": workspace_artifacts,
                "note": "The extracted workspace is not persisted; checkpoint stores prompt, README, task metadata, listing, agent log, and selected generated workspace artifacts.",
            },
        }
        (sample_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        _write_status(
            sample_dir,
            phase="finished",
            harness=harness,
            sample_id=sample_id,
            status=status,
            stop_reason=stop_reason,
            returncode=returncode,
            timed_out=timed_out,
            submission_attempts=len(submissions),
            analysis_produced=analysis_produced,
        )
        print(json.dumps(manifest, indent=2, default=str), flush=True)
        return status
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        try:
            _write_checkpoint(sample_dir, scratch / "workspace", prompt, task_payload, log_path)
        except Exception:
            pass
        manifest = write_failure_artifact(
            sample_dir,
            sample_id=sample_id,
            harness=harness,
            model=args.model,
            framework=(
                "reward_framework"
                if os.getenv("REWARD_FRAMEWORK_RUN_ID")
                else "poc_generation"
            ),
            evaluation_protocol="poc_analysis_artifact_per_submission_v3_cli_local",
            status="error",
            stop_reason="runner_exception",
            error=error,
            returncode=returncode,
            timed_out=timed_out,
            seconds=round(time.monotonic() - started, 3),
            command=command,
            log_path=log_path if log_path.is_file() else None,
            extra={
                "runtime_readiness": runtime_readiness,
                "api_key_env": _manifest_api_key_env(args),
                "base_url_configured": bool(args.base_url),
                "max_iter": args.max_iter,
                "timeout": args.timeout,
                "analysis": {
                    "candidates": analysis_candidates,
                    "final_message_extraction": final_analysis_extraction,
                },
            },
            overwrite_manifest=True,
        )
        _write_status(
            sample_dir,
            phase="failed",
            harness=harness,
            sample_id=sample_id,
            status="error",
            error=error,
            returncode=returncode,
            timed_out=timed_out,
        )
        print(json.dumps(manifest, indent=2, default=str), flush=True)
        return "error"
    finally:
        if bridge is not None:
            bridge.close()
        if bridge_proc is not None:
            bridge_proc.terminate()
            try:
                bridge_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge_proc.kill()
                bridge_proc.wait(timeout=5)
        _cleanup_cli_scratch(scratch)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", required=True, choices=CLI_HARNESSES)
    parser.add_argument("--arvo-id")
    parser.add_argument("--sample-id")
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key-env", default="")
    parser.add_argument("--api-version", default="")
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=10800)
    parser.add_argument("--server", default="http://host.docker.internal:8666")
    parser.add_argument("--difficulty", default="level1")
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--server-root", type=Path, default=GT_ROOT / "harness_runtime" / "server")
    parser.add_argument("--workspace-installer", default="")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--codex-reasoning-effort", default="")
    parser.add_argument("--codex-bridge", default="auto", choices=("auto", "none", "modelhub_crawl"))
    parser.add_argument("--bridge-payload-format", default="auto")
    parser.add_argument("--bridge-caller", default="")
    parser.add_argument("--bridge-disable-proxy", action="store_true")
    args = parser.parse_args()

    args.harness = normalize_harness_name(args.harness)
    args.prompt_file = args.prompt_file.expanduser().resolve()
    args.server_root = args.server_root.expanduser().resolve()
    if not args.prompt_file.is_file():
        parser.error(f"prompt file not found: {args.prompt_file}")
    results_dir = args.results_dir.expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    os.environ["CYBERGYM_PREEXTRACT_REPO_TAR"] = "1"

    if args.sample_id:
        if args.arvo_id:
            parser.error("pass either --sample-id or --arvo-id, not both")
        sample_id = str(args.sample_id).strip()
        if not sample_id or sample_id.startswith("arvo_"):
            parser.error("--sample-id must name a non-ARVO sample")
        last_status = None
        for attempt in range(1, args.max_attempts + 1):
            print(f"[*] {sample_id}: {args.harness} local generation attempt {attempt}/{args.max_attempts}", flush=True)
            last_status = run_local_once(args, sample_id, results_dir)
            sample_dir = results_dir / sample_id
            if last_status in {"success", "agent_finished"} and (sample_dir / "analysis.json").is_file():
                return 0
            print(
                f"[*] {sample_id}: attempt {attempt} did not yield a complete analysis "
                f"(status={last_status}); {'retrying' if attempt < args.max_attempts else 'giving up'}",
                flush=True,
            )
        return 1
    if not args.arvo_id:
        parser.error("either --arvo-id or --sample-id is required")

    task_id = f"arvo:{args.arvo_id}"
    sample_id = f"arvo_{args.arvo_id}"
    sample_dir = results_dir / sample_id
    try:
        ensure_arvo_source(args.arvo_id)
    except Exception as exc:  # noqa: BLE001
        clear_previous_result(sample_dir)
        manifest = write_failure_artifact(
            sample_dir,
            sample_id=sample_id,
            harness=args.harness,
            model=args.model,
            framework=(
                "reward_framework"
                if os.getenv("REWARD_FRAMEWORK_RUN_ID")
                else "poc_generation"
            ),
            evaluation_protocol="poc_analysis_artifact_per_submission_v3_cli",
            status="error",
            stop_reason="arvo_source_unavailable",
            error=f"{type(exc).__name__}: {exc}",
            extra={"arvo_id": args.arvo_id, "task_id": task_id},
            overwrite_manifest=True,
        )
        print(json.dumps(manifest, indent=2, default=str), flush=True)
        return 1

    last_status = None
    for attempt in range(1, args.max_attempts + 1):
        clear_previous_result(sample_dir)
        print(f"[*] {sample_id}: {args.harness} generation attempt {attempt}/{args.max_attempts}", flush=True)
        last_status = run_once(args, sample_id, task_id, results_dir)
        if last_status in {"success", "agent_finished"} and (sample_dir / "analysis.json").is_file():
            return 0
        print(
            f"[*] {sample_id}: attempt {attempt} did not yield a complete analysis "
            f"(status={last_status}); {'retrying' if attempt < args.max_attempts else 'giving up'}",
            flush=True,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
