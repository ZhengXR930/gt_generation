#!/usr/bin/env python3
"""Run one ARVO CyberGym PoC-generation sample with a CLI agent harness.

This is the non-OpenHands counterpart to ``run_sample.py``.  It creates the
same public CyberGym workspace and submit.sh, runs one CLI agent session from
that workspace, then persists submissions/analysis/manifest under the caller's
``--results-dir``.

It is intentionally not a reward-training entrypoint.  Skill packets are loaded
only when ``--skill-packet-dir`` is explicitly provided by a reward-framework
adapter run.
"""

def _ensure_repo_python() -> None:
    import os as _os
    import sys as _sys
    from pathlib import Path as _Path

    if _sys.version_info >= (3, 10):
        return
    repo_root = _Path(__file__).resolve().parents[2]
    venv_python = repo_root / "external" / "OpenHands" / ".venv-openhands" / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"Python >=3.10 required; missing repo venv: {venv_python}")
    _os.execv(str(venv_python), [str(venv_python), *_sys.argv])


_ensure_repo_python()

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
GT_ROOT = ROOT.parents[1]
DEFAULT_POC_RESULTS = ROOT.parent / "poc_results"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(GT_ROOT))
sys.path.insert(0, str(GT_ROOT / "external" / "cybergym" / "src"))

from cybergym.task.arvo_task import generate_arvo_task  # noqa: E402
from cybergym.task.types import TaskConfig, TaskDifficulty  # noqa: E402
from check_success import check as check_success  # noqa: E402
from poc_dedup import deduplicate_submission_attempts  # noqa: E402
from run_sample import (  # noqa: E402
    cleanup_scratch,
    clear_previous_result,
    default_api_key_env,
    ensure_arvo_source,
    load_env_key,
    persist_analysis_artifact,
    persist_submission_attempts,
)
from reward_framework.adapters.agent_skill_export import (  # noqa: E402
    export_native_agent_skills,
    write_bridge_file,
)
from reward_framework.adapters.poc_task_contract import render_poc_task_prompt  # noqa: E402
from reward_framework.adapters.deepseek_harness.install import export_bundle  # noqa: E402
from reward_framework.adapters.poc_generation import normalize_harness_name  # noqa: E402


CODEX_BRIDGE = GT_ROOT / "gt_generation" / "adapters" / "codex" / "modelhub_crawl_bridge.py"


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
    generated README and submit.sh must reference the concrete workspace path.
    """
    for path in (workspace / "README.md", workspace / "submit.sh"):
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


def _base_prompt(sample_id: str, workspace: Path, max_iter: int, skill_enabled: bool, harness: str) -> str:
    del harness  # The task prompt is intentionally harness-invariant.
    return render_poc_task_prompt(
        sample_id=sample_id,
        workspace=str(workspace),
        max_iter=max_iter,
        skill_packet_enabled=skill_enabled,
    )


def _install_skills(harness: str, packet: Path | None, scratch: Path, workspace: Path, env: dict[str, str]) -> list[str]:
    if packet is None:
        return []
    packet = packet.expanduser().resolve()
    if not packet.is_dir():
        raise FileNotFoundError(f"skill packet not found: {packet}")
    if harness == "codex":
        skills_dir = scratch / "codex_home" / "skills"
        export_native_agent_skills(packet, skills_dir, adapter_name="poc_generation_codex")
        env["CODEX_HOME"] = str(skills_dir.parent)
        write_bridge_file(workspace / "reward_framework_codex_skills.md", adapter_name="codex", skills_dir=skills_dir)
        return ["poc-vulnerability-reproduction", "poc-submission-verification"]
    if harness == "claude":
        config_dir = scratch / "claude_config"
        skills_dir = config_dir / "skills"
        export_native_agent_skills(packet, skills_dir, adapter_name="poc_generation_claude")
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        write_bridge_file(workspace / ".claude" / "reward_framework_poc_skills.md", adapter_name="claude", skills_dir=skills_dir)
        return ["poc-vulnerability-reproduction", "poc-submission-verification"]
    if harness == "deepseek_harness":
        bundle = scratch / "dsh_bundle"
        manifest = export_bundle(packet, bundle)
        env["REWARD_FRAMEWORK_DSH_BUNDLE_DIR"] = str(bundle)
        env["REWARD_FRAMEWORK_DSH_PATCH_FILE"] = str(manifest["patch_file"])
        return ["poc-vulnerability-reproduction", "poc-submission-verification"]
    raise ValueError(f"skill install not supported for harness: {harness}")


def _start_codex_bridge(args: argparse.Namespace, env: dict[str, str], sample_dir: Path) -> tuple[subprocess.Popen | None, str]:
    if not args.base_url:
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
        args.base_url,
        "--api-key-env",
        key_env,
        "--max-tokens",
        str(args.max_output_tokens),
        "--timeout-seconds",
        str(args.timeout),
        "--log-file",
        str(log_file),
    ]
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
    bridge_proc, provider_base = _start_codex_bridge(args, env, sample_dir)
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
        provider = "modelhub"
        key_env = args.api_key_env or default_api_key_env(args.model)
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


def _claude_command(args: argparse.Namespace, workspace: Path, prompt: str, env: dict[str, str]) -> tuple[list[str], None]:
    key_env = args.api_key_env or "ANTHROPIC_AUTH_TOKEN"
    _load_api_key(env, key_env)
    if key_env != "ANTHROPIC_AUTH_TOKEN" and env.get(key_env) and not env.get("ANTHROPIC_AUTH_TOKEN"):
        env["ANTHROPIC_AUTH_TOKEN"] = env[key_env]
    if args.base_url:
        env["ANTHROPIC_BASE_URL"] = args.base_url
    else:
        base = _optional_config_value("ANTHROPIC_BASE_URL")
        if base:
            env["ANTHROPIC_BASE_URL"] = base
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    env.setdefault("CLAUDE_CODE_ATTRIBUTION_HEADER", "0")
    return [
        "claude",
        "-p",
        prompt,
        "--allowedTools",
        "Bash Read Write Edit Glob Grep",
        "--add-dir",
        str(workspace),
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--model",
        _claude_model_name(args.model),
    ], None


def _deepseek_command(args: argparse.Namespace, workspace: Path, prompt: str, env: dict[str, str]) -> tuple[list[str], None]:
    key_env = args.api_key_env or "DEEPSEEK_API_KEY"
    _load_api_key(env, key_env)
    if key_env != "DEEPSEEK_API_KEY" and env.get(key_env) and not env.get("DEEPSEEK_API_KEY"):
        env["DEEPSEEK_API_KEY"] = env[key_env]
    if args.base_url:
        env["DEEPSEEK_BASE_URL"] = args.base_url
    env.setdefault("DEEPSEEK_MODEL", args.model)
    env.setdefault("DSH_PERMISSION_MODE", "danger-full-access")
    env.setdefault("DSH_TELEMETRY_MODE", "DISABLED")
    env.setdefault("CHOKIDAR_USEPOLLING", "1")
    env.setdefault("CHOKIDAR_INTERVAL", "1000")
    command = ["dsh", "--profile", args.dsh_profile]
    patch = env.get("REWARD_FRAMEWORK_DSH_PATCH_FILE")
    if patch:
        command += ["--patch", patch]
    command.append(prompt)
    return command, None


def _agent_command(args: argparse.Namespace, workspace: Path, prompt: str, env: dict[str, str], sample_dir: Path) -> tuple[list[str], subprocess.Popen | None]:
    harness = normalize_harness_name(args.harness)
    if harness == "codex":
        return _codex_command(args, workspace, prompt, env, sample_dir)
    if harness == "claude":
        return _claude_command(args, workspace, prompt, env)
    if harness == "deepseek_harness":
        return _deepseek_command(args, workspace, prompt, env)
    raise ValueError(f"unsupported CLI harness: {args.harness}")


def _copy_latest_analysis(workspace: Path, sample_dir: Path) -> tuple[bool, str]:
    for candidate in (workspace / ".latest_analysis.json", workspace / "analysis.json"):
        if candidate.is_file() and persist_analysis_artifact(candidate, sample_dir):
            return True, str(candidate.relative_to(workspace) if candidate.is_relative_to(workspace) else candidate)
    return (sample_dir / "analysis.json").is_file(), "existing" if (sample_dir / "analysis.json").is_file() else "none"


def _write_checkpoint(sample_dir: Path, workspace: Path, prompt: str, task_payload: dict[str, Any], log_path: Path) -> None:
    checkpoint = sample_dir / "checkpoint"
    if checkpoint.exists():
        shutil.rmtree(checkpoint)
    checkpoint.mkdir(parents=True)
    (checkpoint / "prompt.txt").write_text(prompt, encoding="utf-8")
    (checkpoint / "task.json").write_text(json.dumps(task_payload, indent=2, default=str) + "\n", encoding="utf-8")
    (checkpoint / "workspace_listing.txt").write_text(_workspace_listing(workspace), encoding="utf-8")
    if (workspace / "README.md").is_file():
        shutil.copy2(workspace / "README.md", checkpoint / "README.md")
    if log_path.is_file():
        shutil.copy2(log_path, checkpoint / "agent.log")


def run_once(args: argparse.Namespace, sample_id: str, task_id: str, results_dir: Path) -> str:
    harness = normalize_harness_name(args.harness)
    sample_dir = results_dir / sample_id
    scratch = Path(tempfile.mkdtemp(prefix=f"run_{sample_id}_{harness}_"))
    workspace = scratch / "workspace"
    workspace.mkdir(parents=True)
    run_dir = sample_dir / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{harness}_{int(time.time())}.log"
    bridge_proc: subprocess.Popen | None = None
    started = time.monotonic()
    timed_out = False
    returncode: int | None = None
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
        _rewrite_workspace_paths(workspace)
        env = os.environ.copy()
        skill_packet = Path(args.skill_packet_dir).expanduser().resolve() if args.skill_packet_dir else None
        installed_skills = _install_skills(harness, skill_packet, scratch, workspace, env)
        prompt = _base_prompt(sample_id, workspace, args.max_iter, bool(installed_skills), harness)
        command, bridge_proc = _agent_command(args, workspace, prompt, env, sample_dir)
        task_payload = {
            "task_id": task.task_id,
            "agent_id": task.agent_id,
            "checksum": task.checksum,
            "server": task.server,
            "difficulty": str(task.difficulty),
            "harness": harness,
            "model": args.model,
            "installed_skills": installed_skills,
        }
        with log_path.open("w", encoding="utf-8") as stream:
            stream.write("COMMAND " + json.dumps(command, ensure_ascii=False) + "\n")
            stream.flush()
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
        db_path = ROOT / "server" / "poc.db"
        success_info = check_success(db_path, task.agent_id) if db_path.exists() else {"ok": False, "error": "db not found", "submission_attempts": [], "success": False}
        fallback_analysis = workspace / ".latest_analysis.json"
        if not fallback_analysis.is_file():
            fallback_analysis = workspace / "analysis.json"
        persisted_attempts = persist_submission_attempts(
            sample_dir,
            task.agent_id,
            success_info.get("submission_attempts") or [],
            fallback_analysis if fallback_analysis.is_file() else None,
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
            produced, source = _copy_latest_analysis(workspace, sample_dir)
            if produced:
                analysis_source = source
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
        manifest = {
            "evaluation_protocol": "poc_analysis_artifact_per_submission_v3_cli",
            "arvo_id": args.arvo_id,
            "task_id": task_id,
            "sample_id": sample_id,
            "cybergym_agent_id": task.agent_id,
            "model": args.model,
            "harness": harness,
            "api_key_env": args.api_key_env or default_api_key_env(args.model),
            "base_url_configured": bool(args.base_url),
            "max_iter": args.max_iter,
            "timeout": args.timeout,
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "stop_reason": stop_reason,
            "seconds": round(time.monotonic() - started, 3),
            "skill_packet": str(skill_packet) if skill_packet else None,
            "installed_skills": installed_skills,
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
            },
            "checkpoint": {
                "dir": "checkpoint/",
                "phase": "cli_terminal",
                "contains_workspace_listing": True,
                "note": "The extracted workspace is not persisted; checkpoint stores prompt, README, task metadata, listing, and agent log.",
            },
        }
        (sample_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(manifest, indent=2, default=str), flush=True)
        return status
    finally:
        if bridge_proc is not None:
            bridge_proc.terminate()
            try:
                bridge_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge_proc.kill()
                bridge_proc.wait(timeout=5)
        cleanup_scratch(scratch)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", required=True, choices=("codex", "claude", "deepseek_harness", "dsh", "deepseek"))
    parser.add_argument("--arvo-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key-env", default="")
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=10800)
    parser.add_argument("--server", default="http://host.docker.internal:8666")
    parser.add_argument("--difficulty", default="level1")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_POC_RESULTS)
    parser.add_argument("--skill-packet-dir", default="")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--codex-reasoning-effort", default="")
    parser.add_argument("--dsh-profile", default="headless")
    args = parser.parse_args()

    args.harness = normalize_harness_name(args.harness)
    results_dir = args.results_dir.expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    os.environ["CYBERGYM_PREEXTRACT_REPO_TAR"] = "1"

    task_id = f"arvo:{args.arvo_id}"
    sample_id = f"arvo_{args.arvo_id}"
    ensure_arvo_source(args.arvo_id)
    sample_dir = results_dir / sample_id

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
