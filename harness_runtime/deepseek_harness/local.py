#!/usr/bin/env python3
"""DeepSeek Harness runner for local non-CyberGym PoC generation samples.

This backend runs DeepSeek Harness on the host, not inside Docker.  The harness
can still invoke the existing local build/submit bridge, whose validator runs
the vulnerable target inside the benchmark Docker image.  This avoids a
docker-in-docker topology while preserving the same result package layout used
by the OpenHands local runner:

  - manifest.json
  - analysis.json, if the agent produced/submitted a valid analysis artifact
  - submissions/<attempt>/ with poc.bin, analysis.json, result.json,
    runtime_output.txt
  - checkpoint/ with prompt/config/args plus raw DSH session state

The first supported scope is non-ARVO samples.  ARVO/CyberGym needs a separate
server-side submit bridge and is intentionally not routed here.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

DSH_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = DSH_ROOT.parent
GT_ROOT = RUNTIME_ROOT.parent
NETWORK_GUARD_SOURCE = DSH_ROOT / "network_guard.c"
NETWORK_GUARD_CACHE = Path(
    os.environ.get(
        "GT_GENERATION_NETWORK_GUARD_CACHE",
        str(Path.home() / ".cache" / "gt_generation_network_guard"),
    )
)
DEFAULT_DSH_HOME = Path(
    os.environ.get(
        "GT_GENERATION_DSH_HOME",
        str(Path.home() / ".cache" / "gt_generation_deepseek_harness_home"),
    )
)
DEFAULT_DSH_NODE_ROOT = Path(
    os.environ.get(
        "GT_GENERATION_DSH_NODE_ROOT",
        str(Path.home() / ".local" / "node-v24-musl"),
    )
)
DEFAULT_DSH_SCRATCH_ROOT = Path(
    os.environ.get(
        "GT_GENERATION_DSH_SCRATCH_ROOT",
        str(Path.home() / ".cache" / "gt_generation_dsh_scratch"),
    )
)

sys.path.insert(0, str(RUNTIME_ROOT))
sys.path.insert(0, str(GT_ROOT))

from harness_runtime.python_env import ensure_repo_python  # noqa: E402

ensure_repo_python(GT_ROOT, min_version=(3, 11))

from evaluator.reasoning.analysis_artifact import validate_analysis_artifact_quality  # noqa: E402
from harness_runtime.dedup import deduplicate_submission_attempts  # noqa: E402
from harness_runtime.failure_artifact import write_failure_artifact  # noqa: E402
from harness_runtime.openhands.local import (  # noqa: E402
    LocalExecutionBridge,
    check_runtime_readiness,
    clear_previous_result,
    load_runtime_spec,
    persist_results,
    prepare_workspace,
    validate_submissions_on_host,
    write_build_sh,
    write_submit_sh,
)
from harness_runtime.auth import load_env_key  # noqa: E402
from harness_runtime.deepseek_harness.reachability import (  # noqa: E402
    DEFAULT_REACHABILITY_LOCK_DIR,
    run_reachability_pipeline,
)
from harness_runtime.workspace import render_prompt, run_workspace_installer  # noqa: E402


def cleanup_dsh_scratch(scratch: Path, scratch_root: Path) -> None:
    """Remove this runner's temporary workspace with a conservative path guard."""
    scratch = scratch.resolve()
    scratch_root = scratch_root.resolve()
    allowed_prefixes = ("run_dsh_local_", "run_dsh_arvo_", "run_dsh_finalize_")
    if scratch.parent != scratch_root or not scratch.name.startswith(allowed_prefixes):
        print(
            f"[dsh-runner] refusing to clean unexpected scratch path: {scratch}",
            file=sys.stderr,
        )
        return
    shutil.rmtree(scratch, ignore_errors=True)


def docker_bridge_gateway() -> str:
    try:
        inspect = subprocess.run(
            ["docker", "network", "inspect", "bridge"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if inspect.returncode == 0:
            networks = json.loads(inspect.stdout)
            configs = (
                ((networks[0] or {}).get("IPAM") or {}).get("Config") or []
            )
            for item in configs:
                gateway = str(item.get("Gateway") or "").strip()
                if gateway:
                    return gateway
    except Exception:
        pass
    return "172.17.0.1"


def compile_network_guard() -> Path:
    """Build the musl LD_PRELOAD guard once in a shared cache."""
    guard_so = NETWORK_GUARD_CACHE / "network_guard.so"
    if guard_so.is_file():
        return guard_so
    NETWORK_GUARD_CACHE.mkdir(parents=True, exist_ok=True)
    command = (
        "apk add --no-cache build-base >/dev/null && "
        "gcc -shared -fPIC -O2 -Wall -Wextra "
        "-o /out/network_guard.so /src/network_guard.c -ldl && "
        f"chown {os.getuid()}:{os.getgid()} /out/network_guard.so"
    )
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{DSH_ROOT}:/src:ro",
            "-v",
            f"{NETWORK_GUARD_CACHE}:/out",
            "alpine:3.23",
            "sh",
            "-lc",
            command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if completed.returncode != 0 or not guard_so.is_file():
        raise RuntimeError(
            "failed to build network guard; refusing to run with tool network "
            f"enabled\n{completed.stdout[-4000:]}"
        )
    return guard_so


def create_network_guard_bin(
    scratch: Path, guard_so: Path, allowed_hosts: list[str]
) -> Path:
    """Prepend wrappers that confine model-facing shell descendants."""
    bindir = scratch / "network_guard_bin"
    bindir.mkdir()
    allow = ",".join(dict.fromkeys(host for host in allowed_hosts if host))
    bash = bindir / "bash"
    bash.write_text(
        "#!/bin/sh\n"
        f"export LD_PRELOAD={guard_so}"
        '${LD_PRELOAD:+:$LD_PRELOAD}\n'
        f"export POCGEN_NETWORK_GUARD_ALLOW='{allow}'\n"
        "exec /bin/bash \"$@\"\n",
        encoding="utf-8",
    )
    bash.chmod(0o755)
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "echo '[network_guard] docker is disabled for the subject-agent shell; "
        "use ./build.sh and ./submit.sh' >&2\n"
        "exit 126\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bindir


def network_guard_allowed_hosts(validator_url: str) -> list[str]:
    hosts = ["127.0.0.1", "::1", docker_bridge_gateway()]
    parsed = urlparse(validator_url)
    if parsed.hostname:
        hosts.append(parsed.hostname)
    return list(dict.fromkeys(hosts))


def adapt_readme_for_host_workspace(workspace: Path) -> None:
    """Make the local README path instructions match DSH's host cwd."""
    readme = workspace / "README.md"
    text = readme.read_text(encoding="utf-8")
    workspace_text = str(workspace)
    text = text.replace("/workspace/", f"{workspace_text}/")
    text = text.replace("/workspace", workspace_text)
    readme.write_text(text, encoding="utf-8")


def _is_public_testcase_entry(name: str) -> bool:
    normalized = name.replace("\\", "/").lower()
    basename = normalized.rsplit("/", 1)[-1]
    parts = [part for part in normalized.split("/") if part]
    return (
        "/poc/" in f"/{normalized}"
        or basename.startswith("poc-")
        or basename.startswith("clusterfuzz-testcase")
        or basename.startswith("crash-")
        or basename.startswith("crasher-")
        or "clusterfuzz-testcase" in basename
        or ("testcase" in basename and any(part in {"crashes", "crashers", "poc", "pocs"} for part in parts))
    )


def scrub_agent_visible_public_testcases(workspace: Path) -> dict:
    """Remove public PoC/testcase corpus artifacts from the subject workspace.

    Benchmark agents should see the issue description and vulnerable codebase,
    not bundled crash reproducers or fuzzing seed archives.  This scrub operates
    only on the per-run copied workspace; the source dataset is untouched.
    """
    removed: list[dict] = []
    errors: list[dict] = []
    candidates: list[Path] = []
    roots = [workspace / "repo-vul" / "src-vul", workspace / "repo-vul", workspace]
    seen_roots: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        resolved = root.resolve()
        if any(resolved == parent or parent in resolved.parents for parent in seen_roots):
            continue
        seen_roots.add(resolved)
        try:
            candidates.extend(root.rglob("*"))
        except OSError as exc:
            errors.append({"path": str(root), "error": str(exc)})

    to_remove: dict[Path, str] = {}
    for path in candidates:
        try:
            relative = path.relative_to(workspace).as_posix()
        except ValueError:
            relative = path.as_posix()
        lower_relative = relative.lower()
        name = path.name.lower()
        parts = [part.lower() for part in path.parts]
        suspicious_context = any(
            part in {"testdata", "tests", "fuzz", "fuzzer", "corpus", "seed_corpus"}
            for part in parts
        )
        try:
            is_symlink = path.is_symlink()
            is_dir = path.is_dir() if not is_symlink else False
            is_file = path.is_file() if not is_symlink else False
        except OSError as exc:
            errors.append({"path": relative, "error": f"path inspection failed: {exc}"})
            continue
        if is_dir:
            if name in {"poc", "pocs", "crashes", "crashers"} and suspicious_context:
                to_remove[path] = "public testcase directory"
            continue
        if not is_file:
            continue
        if _is_public_testcase_entry(relative) and suspicious_context:
            to_remove[path] = "public testcase file"
            continue
        suffixes = "".join(path.suffixes).lower()
        if suffixes == ".zip":
            remove_zip = "seed_corpus" in name or "corpus" in name or "poc" in name or "crash" in name
            zip_reason = "public testcase archive name" if remove_zip else ""
            try:
                with zipfile.ZipFile(path) as archive:
                    for member in archive.namelist():
                        if _is_public_testcase_entry(member):
                            remove_zip = True
                            zip_reason = f"public testcase archive member: {member[:120]}"
                            break
            except (OSError, zipfile.BadZipFile) as exc:
                errors.append({"path": relative, "error": f"zip inspection failed: {exc}"})
            if remove_zip:
                to_remove[path] = zip_reason
                continue
        elif suffixes in {".tar", ".tgz", ".tar.gz", ".tar.xz", ".tar.bz2", ".7z"}:
            if "seed_corpus" in name or "corpus" in name or "poc" in name or "crash" in name:
                to_remove[path] = "public testcase archive name"
                continue

    # Remove children before parents.
    for path, reason in sorted(to_remove.items(), key=lambda item: len(item[0].parts), reverse=True):
        if not path.exists() and not path.is_symlink():
            continue
        try:
            relative = path.relative_to(workspace).as_posix()
        except ValueError:
            relative = path.as_posix()
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                kind = "dir"
            else:
                path.unlink()
                kind = "file"
            removed.append({"path": relative, "kind": kind, "reason": reason})
        except OSError as exc:
            errors.append({"path": relative, "error": str(exc)})

    return {
        "enabled": True,
        "policy": "remove agent-visible public PoC/testcase/corpus artifacts from per-run workspace",
        "removed_count": len(removed),
        "removed": removed,
        "errors": errors,
    }


def write_dsh_settings(dsh_home: Path, model: str, reasoning_effort: str) -> None:
    """Pin the DSH profile used by evaluation runs.

    The profile is intentionally deterministic for benchmark fairness:
    - JSONL sessions are stored uncompressed under sessions-jsonl/ so the runner
      can enforce an iteration cap from the live trajectory.
    - packed assistant chunks are disabled so each SessionEvent is one JSON line.
    - parallel tool execution is capped at one to match OpenHands-style
      sequential iteration accounting.
    - web search is disabled.  The subject agent may inspect the provided issue
      description and codebase, but must not search for public PoCs/testcases.
    - harness-native subagents are not disabled; their calls are charged to and
      reported under the same sample budget.
    """
    dsh_home.mkdir(parents=True, exist_ok=True)
    payload = {
        "agent-default-model": {
            "provider": "deepseek-official",
            "model": model,
        }
    }
    if reasoning_effort:
        payload["agent-default-model"]["reasoningEffort"] = reasoning_effort
    (dsh_home / "settings.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    profile_dir = dsh_home / "profiles" / "headless"
    profile_dir.mkdir(parents=True, exist_ok=True)
    package_json = profile_dir / "package.json"
    if not package_json.is_file():
        package_json.write_text(
            json.dumps(
                {
                    "name": "dsh-profile-headless",
                    "private": True,
                    "dependencies": {},
                    "dsh": {
                        "profile": {
                            "bundles": [
                                "@deepseek-ai/dsh-base",
                                "@deepseek-ai/dsh-headless",
                            ]
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    cordis_yml = profile_dir / "cordis.yml"
    if not cordis_yml.is_file():
        cordis_yml.write_text("[]\n", encoding="utf-8")
    workspace_yml = profile_dir / "pnpm-workspace.yaml"
    if not workspace_yml.is_file():
        workspace_yml.write_text(
            "packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n",
            encoding="utf-8",
        )
    (profile_dir / "cordis.patch.yml").write_text(
        "\n".join(
            [
                "- id: session-persistence-jsonl",
                "  config:",
                "    root: !!js dshHomePath('sessions-jsonl')",
                "    packChunks: false",
                "    compression: none",
                "- id: agent-loop",
                "  config:",
                "    maxParallelToolCalls: 1",
                "- id: tool-web",
                "  disabled: true",
                "",
            ]
        ),
        encoding="utf-8",
    )


def extract_json_objects(text: str) -> list[dict]:
    """Best-effort extraction of JSON objects from a final assistant message."""
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def persist_final_stdout_analysis(
    stdout_path: Path, sample_result_dir: Path, sample_id: str
) -> dict:
    """Persist a valid final analysis JSON from DSH stdout when no PoC was submitted."""
    if (sample_result_dir / "analysis.json").is_file() or not stdout_path.is_file():
        return {"produced": (sample_result_dir / "analysis.json").is_file()}
    text = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
    candidates: list[dict] = []
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                candidates.append(parsed)
        except json.JSONDecodeError:
            pass
        candidates.extend(extract_json_objects(text))

    for candidate in reversed(candidates):
        if candidate.get("sample_id") != sample_id:
            continue
        raw = json.dumps(candidate, ensure_ascii=False)
        error = validate_analysis_artifact_quality(raw)
        if error is not None:
            continue
        (sample_result_dir / "analysis.json").write_text(
            json.dumps(candidate, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return {"produced": True, "source": "dsh_final_stdout"}
    return {"produced": False, "source": "missing_or_invalid_dsh_final_stdout"}


def list_dsh_session_files(dsh_home: Path) -> set[Path]:
    files: set[Path] = set()
    for name in ("sessions-jsonl", "sessions"):
        sessions = dsh_home / name
        if sessions.is_dir():
            files.update(path.resolve() for path in sessions.rglob("*") if path.is_file())
    return files


def count_dsh_completed_steps(session_files: set[Path]) -> int:
    """Count completed DSH agent steps from plain JSONL session logs.

    DSH has one outer turn for a headless task and many inner steps.  The inner
    `step/end` event is the closest unit to OpenHands' iteration counter.
    """
    count = 0
    for path in sorted(session_files):
        if path.name != "session.jsonl":
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "step/end":
                        count += 1
        except OSError:
            continue
    return count


def summarize_dsh_sessions(session_files: set[Path]) -> dict:
    """Summarize DSH trajectory usage for benchmark fairness auditing.

    We do not forbid harness-native delegation, because Codex/Claude Code/DSH
    differ in how they expose it.  Instead, every visible delegated action is
    reported and all session steps remain charged to the same sample-level cap.
    """
    event_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    step_starts = 0
    step_ends = 0
    subagent_calls = 0
    web_search_calls = 0
    malformed_jsonl = 0
    session_jsonl_files = 0

    for path in sorted(session_files):
        if path.name != "session.jsonl":
            continue
        session_jsonl_files += 1
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_jsonl += 1
                        continue
                    event_type = event.get("type")
                    if isinstance(event_type, str):
                        event_counts[event_type] += 1
                    if event_type == "step/start":
                        step_starts += 1
                    elif event_type == "step/end":
                        step_ends += 1
                    elif event_type == "tool/call":
                        data = event.get("data")
                        name = data.get("name") if isinstance(data, dict) else None
                        if isinstance(name, str):
                            tool_counts[name] += 1
                            lowered = name.lower()
                            if "subagent" in lowered:
                                subagent_calls += 1
                            if name == "web_search" or "web_search" in lowered or lowered == "web":
                                web_search_calls += 1
        except OSError:
            continue

    return {
        "budget_unit": "dsh_step_end",
        "session_jsonl_files": session_jsonl_files,
        "step_starts": step_starts,
        "completed_steps": step_ends,
        "event_counts": dict(sorted(event_counts.items())),
        "tool_calls": sum(tool_counts.values()),
        "tool_counts": dict(sorted(tool_counts.items())),
        "subagent_calls": subagent_calls,
        "web_search_calls": web_search_calls,
        "malformed_jsonl_lines": malformed_jsonl,
        "subagents_allowed": True,
        "subagents_charged_to_sample_budget": True,
        "tool_parallelism_limit": 1,
        "web_search_allowed": False,
    }


def dsh_workspace_session_slug(workspace: Path) -> str:
    """Return the session directory slug DeepSeek Harness derives from cwd."""
    resolved = str(workspace.resolve()).strip("/")
    return f"--{resolved.replace('/', '-')}--"


def filter_dsh_session_files_for_workspace(
    session_files: set[Path], workspace: Path
) -> set[Path]:
    """Keep only session files produced for this runner's workspace.

    DSH stores sessions under a cwd-derived path such as:
      sessions/--home-xinran-...-run_dsh_arvo_arvo_10129_xxx-workspace--/...

    The batch runner shares one DSH_HOME across parallel samples, so a plain
    "current - preexisting" set can include sessions created by sibling samples.
    """
    slug = dsh_workspace_session_slug(workspace)
    return {path for path in session_files if slug in path.as_posix()}


def copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists() and not src.is_symlink():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        target = os.readlink(src)
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        os.symlink(target, dst)
    elif src.is_file():
        shutil.copy2(src, dst)


def copy_dsh_checkpoint(
    dsh_home: Path, sample_result_dir: Path, session_files: set[Path]
) -> None:
    """Preserve reproducible DSH state without copying runtime dependencies."""
    if not dsh_home.exists():
        return
    checkpoint = sample_result_dir / "checkpoint"
    checkpoint.mkdir(parents=True, exist_ok=True)
    dst = checkpoint / "dsh_home"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir()
    for name in (".anonymous-user-id", "settings.json", "settings.yaml"):
        copy_if_exists(dsh_home / name, dst / name)
    profiles = dsh_home / "profiles"
    if profiles.is_dir():
        for path in profiles.rglob("*"):
            if path.name in {"node_modules", ".pnpm"}:
                continue
            if any(part in {"node_modules", ".pnpm"} for part in path.parts):
                continue
            if path.is_file() or path.is_symlink():
                copy_if_exists(path, dst / path.relative_to(dsh_home))
    for path in sorted(session_files):
        try:
            relative = path.relative_to(dsh_home)
        except ValueError:
            continue
        copy_if_exists(path, dst / relative)


def slim_dsh_checkpoint_if_analysis_valid(sample_result_dir: Path) -> dict:
    """Drop raw DSH sessions once a valid top-level analysis artifact exists."""
    analysis_path = sample_result_dir / "analysis.json"
    sessions = sample_result_dir / "checkpoint" / "dsh_home" / "sessions-jsonl"
    result = {
        "enabled": True,
        "analysis_valid": False,
        "sessions_removed": False,
        "bytes_removed": 0,
    }
    if not analysis_path.is_file():
        result["reason"] = "missing_top_level_analysis"
        return result
    error = validate_analysis_artifact_quality(
        analysis_path.read_text(encoding="utf-8", errors="replace")
    )
    if error is not None:
        result["reason"] = f"invalid_analysis: {error}"
        return result
    result["analysis_valid"] = True
    if not sessions.is_dir():
        result["reason"] = "no_sessions_jsonl_dir"
        return result
    result["bytes_removed"] = sum(path.stat().st_size for path in sessions.rglob("*") if path.is_file())
    shutil.rmtree(sessions)
    result["sessions_removed"] = True
    (sample_result_dir / "checkpoint_slim.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result


def run_dsh(
    *,
    dsh_src: Path,
    node_root: Path,
    dsh_home: Path,
    workspace: Path,
    prompt_path: Path,
    run_dir: Path,
    api_key: str,
    base_url: str,
    timeout: int,
    network_guard_bin: Path | None,
    stop_when: Callable[[], str | None] | None = None,
    stop_poll_seconds: float = 5.0,
) -> tuple[int | None, bool, str | None, float]:
    node = node_root / "bin" / "node"
    cli = dsh_src / "apps" / "cli" / "lib" / "bin.js"
    if not node.is_file():
        raise RuntimeError(f"Node runtime not found: {node}")
    if not cli.is_file():
        raise RuntimeError(f"DeepSeek Harness CLI not built: {cli}")

    env = os.environ.copy()
    path_entries = [str(node_root / "bin"), env.get("PATH", "")]
    if network_guard_bin is not None:
        path_entries.insert(0, str(network_guard_bin))
    env["PATH"] = ":".join(entry for entry in path_entries if entry)
    env["DSH_HOME"] = str(dsh_home)
    env["DSH_PERMISSION_MODE"] = "danger-full-access"
    env["DSH_TELEMETRY_DISABLED"] = "1"
    env["DEEPSEEK_API_KEY"] = api_key
    if base_url:
        env["DEEPSEEK_BASE_URL"] = base_url

    command = [
        str(node),
        "--expose-internals",
        str(cli),
        "--profile",
        "headless",
    ]
    patch_file = env.get("HARNESS_DSH_PATCH_FILE", "").strip()
    if patch_file:
        command.extend(["--patch", patch_file])
    command.append(prompt_path.read_text(encoding="utf-8"))
    started = time.monotonic()
    stdout_path = run_dir / "dsh_stdout.txt"
    stderr_path = run_dir / "dsh_stderr.txt"
    timed_out = False
    stop_reason_hit: str | None = None
    returncode: int | None
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        deadline = started + timeout
        next_stop_check = started
        while True:
            returncode = process.poll()
            if returncode is not None:
                break
            now = time.monotonic()
            if stop_when is not None and now >= next_stop_check:
                next_stop_check = now + stop_poll_seconds
                try:
                    reason = stop_when()
                    if reason is not None:
                        stop_reason_hit = reason
                        stderr.write(
                            f"\n[dsh-runner] stopped early: {reason}\n"
                        )
                        stderr.flush()
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            returncode = process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            returncode = process.wait()
                        break
                except Exception as exc:  # keep the subject run alive on monitor errors
                    stderr.write(f"\n[dsh-runner] stop monitor failed: {exc}\n")
                    stderr.flush()
            if now >= deadline:
                timed_out = True
                stderr.write(f"\n[dsh-runner] timed out after {timeout} seconds\n")
                stderr.flush()
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    returncode = process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    returncode = process.wait()
                break
            time.sleep(min(1.0, max(0.05, deadline - now)))
    return returncode, timed_out, stop_reason_hit, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-version", default="")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument(
        "--dsh-src",
        type=Path,
        default=GT_ROOT / "external" / "deepseek-harness",
    )
    parser.add_argument(
        "--node-root",
        type=Path,
        default=DEFAULT_DSH_NODE_ROOT,
    )
    parser.add_argument(
        "--dsh-home",
        type=Path,
        default=DEFAULT_DSH_HOME,
        help=(
            "Shared host-side DeepSeek Harness home.  Runtime dependencies live "
            "here; each result checkpoint copies only this run's session/config."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        default="max",
        choices=("off", "high", "max"),
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--scratch-root",
        type=Path,
        default=DEFAULT_DSH_SCRATCH_ROOT,
        help="Directory for per-run temporary workspaces; cleaned after each run.",
    )
    parser.add_argument(
        "--allow-tool-network",
        action="store_true",
        help="Debug only: do not inject the subject-agent network guard.",
    )
    parser.add_argument(
        "--run-reachability-after-generation",
        action="store_true",
        default=True,
        help="Run per-sample reachability immediately after PoC generation.",
    )
    parser.add_argument(
        "--no-run-reachability-after-generation",
        dest="run_reachability_after_generation",
        action="store_false",
    )
    parser.add_argument("--reachability-timeout", type=int, default=420)
    parser.add_argument("--reachability-debugger-image", default="gt-memory-env:latest")
    parser.add_argument("--reachability-max-hits-per-event", type=int, default=64)
    parser.add_argument("--reachability-concurrency", type=int, default=1)
    parser.add_argument(
        "--reachability-lock-dir",
        type=Path,
        default=DEFAULT_REACHABILITY_LOCK_DIR,
    )
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--workspace-installer", default="")
    args = parser.parse_args()
    args.prompt_file = args.prompt_file.expanduser().resolve()
    if not args.prompt_file.is_file():
        parser.error(f"prompt file not found: {args.prompt_file}")

    results_dir = args.results_dir.expanduser().resolve()
    sample_result_dir = results_dir / args.sample_id
    gt_sample_dir = GT_ROOT / "gt_results" / args.sample_id
    clear_previous_result(sample_result_dir)
    sample_result_dir.mkdir(parents=True, exist_ok=True)
    framework = (
        "reward_framework"
        if os.getenv("REWARD_FRAMEWORK_RUN_ID")
        else "poc_generation"
    )

    if args.sample_id.startswith("arvo_"):
        manifest = write_failure_artifact(
            sample_result_dir,
            sample_id=args.sample_id,
            harness="deepseek_harness",
            model=args.model,
            framework=framework,
            evaluation_protocol="poc_analysis_artifact_per_submission_v3_dsh_local",
            status="error",
            stop_reason="unsupported_sample_scope",
            error="deepseek_harness local backend supports non-ARVO samples only",
            overwrite_manifest=True,
        )
        print(json.dumps(manifest, indent=2, default=str))
        return 1

    try:
        runtime_readiness = check_runtime_readiness(gt_sample_dir)
    except Exception as exc:  # noqa: BLE001
        manifest = write_failure_artifact(
            sample_result_dir,
            sample_id=args.sample_id,
            harness="deepseek_harness",
            model=args.model,
            framework=framework,
            evaluation_protocol="poc_analysis_artifact_per_submission_v3_dsh_local",
            status="error",
            stop_reason="runtime_readiness_failed",
            error=f"{type(exc).__name__}: {exc}",
            extra={"gt_sample_dir": str(gt_sample_dir)},
            overwrite_manifest=True,
        )
        print(json.dumps(manifest, indent=2, default=str))
        return 1

    scratch_root = args.scratch_root.expanduser().resolve()
    try:
        scratch_root.mkdir(parents=True, exist_ok=True)
        scratch = Path(
            tempfile.mkdtemp(prefix=f"run_dsh_local_{args.sample_id}_", dir=scratch_root)
        )
    except Exception as exc:  # noqa: BLE001
        manifest = write_failure_artifact(
            sample_result_dir,
            sample_id=args.sample_id,
            harness="deepseek_harness",
            model=args.model,
            framework=framework,
            evaluation_protocol="poc_analysis_artifact_per_submission_v3_dsh_local",
            status="error",
            stop_reason="scratch_unavailable",
            error=f"{type(exc).__name__}: {exc}",
            extra={
                "gt_sample_dir": str(gt_sample_dir),
                "scratch_root": str(scratch_root),
                "runtime_readiness": runtime_readiness,
            },
            overwrite_manifest=True,
        )
        print(json.dumps(manifest, indent=2, default=str))
        return 1
    workspace = scratch / "workspace"
    run_dir = scratch / "results" / f"{args.sample_id}-{uuid.uuid4().hex}"
    dsh_home = args.dsh_home.expanduser().resolve()
    prompt_path = scratch / "prompt.txt"
    config_path = scratch / "dsh_config.json"
    preexisting_sessions: set[Path] = set()
    adapter_metadata = None
    public_testcase_scrub: dict = {}
    try:
        workspace, inner_command, repro = prepare_workspace(args.sample_id, scratch)
        adapt_readme_for_host_workspace(workspace)
        public_testcase_scrub = scrub_agent_visible_public_testcases(workspace)
        run_dir.mkdir(parents=True)
        for name in ("file", "cache"):
            (run_dir / name).mkdir()

        preexisting_sessions = list_dsh_session_files(dsh_home)
        write_dsh_settings(dsh_home, args.model, args.reasoning_effort)

        prompt_path.write_text(
            render_prompt(
                args.prompt_file, sample_id=args.sample_id, workspace=workspace
            ),
            encoding="utf-8",
        )
        (run_dir / "args.json").write_text(
            json.dumps(
                {
                    "agent": f"deepseek-harness:{args.model}",
                    "sample_id": args.sample_id,
                    "workspace": str(workspace),
                    "reproduction_inner_command": inner_command,
                    "dsh_home": str(dsh_home),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        bridge = LocalExecutionBridge(workspace, inner_command, repro)
        network_guard_bin = None
        network_guard_manifest = {"mode": "allowed"}
        bridge.start()
        try:
            write_build_sh(workspace, bridge.url, bridge.token)
            write_submit_sh(workspace, bridge.url, bridge.token)
            adapter_metadata = run_workspace_installer(
                args.workspace_installer,
                harness="deepseek_harness",
                workspace=workspace,
                sample_id=args.sample_id,
                scratch=scratch,
                env=os.environ,
            )
            config_path.write_text(
                json.dumps(
                    {
                        "harness": "deepseek_harness",
                        "profile": "headless",
                        "model": args.model,
                        "reasoning_effort": args.reasoning_effort,
                        "base_url_configured": bool(args.base_url),
                        "api_version_ignored": bool(args.api_version),
                        "dsh_src": str(args.dsh_src.expanduser().resolve()),
                        "node_root": str(args.node_root.expanduser().resolve()),
                        "dsh_home": str(dsh_home),
                        "checkpoint_policy": "copy_settings_profiles_and_new_sessions_only",
                        "workspace_adapter": adapter_metadata,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            if not args.allow_tool_network:
                guard_so = compile_network_guard()
                allowed_hosts = network_guard_allowed_hosts(bridge.url)
                network_guard_bin = create_network_guard_bin(
                    scratch, guard_so, allowed_hosts
                )
                network_guard_manifest = {
                    "mode": "blocked_except_local_validator",
                    "guard_so": str(guard_so),
                    "allowed_hosts": allowed_hosts,
                    "blocked": [
                        "external IPv4/IPv6 connect/sendto",
                        "docker/containerd unix sockets",
                        "docker CLI via PATH wrapper",
                    ],
                }
            def stop_after_iteration_cap() -> str | None:
                session_files = filter_dsh_session_files_for_workspace(
                    list_dsh_session_files(dsh_home), workspace
                )
                completed_steps = count_dsh_completed_steps(session_files)
                if completed_steps >= args.max_iter:
                    return f"iteration_cap:{completed_steps}"
                return None

            returncode, timed_out, stop_reason_hit, seconds = run_dsh(
                dsh_src=args.dsh_src.expanduser().resolve(),
                node_root=args.node_root.expanduser().resolve(),
                dsh_home=dsh_home,
                workspace=workspace,
                prompt_path=prompt_path,
                run_dir=run_dir,
                api_key=load_env_key(args.api_key_env),
                base_url=args.base_url,
                timeout=args.timeout,
                network_guard_bin=network_guard_bin,
                stop_when=stop_after_iteration_cap,
                stop_poll_seconds=1.0,
            )
        finally:
            bridge.close()

        submissions = validate_submissions_on_host(
            gt_sample_dir, workspace, inner_command
        )
        current_sessions = list_dsh_session_files(dsh_home)
        new_session_files = filter_dsh_session_files_for_workspace(
            current_sessions - preexisting_sessions, workspace
        )
        if not new_session_files and current_sessions:
            new_session_files = filter_dsh_session_files_for_workspace(
                current_sessions, workspace
            )
        submission_dirs = (
            sorted((workspace / ".submissions").glob("*"))
            if (workspace / ".submissions").is_dir()
            else []
        )
        final_analysis = persist_final_stdout_analysis(
            run_dir / "dsh_stdout.txt", sample_result_dir, args.sample_id
        )
        latest_analysis_exists = (workspace / ".latest_analysis.json").is_file()
        analysis_produced = (
            final_analysis.get("produced") is True
            or latest_analysis_exists
            or (sample_result_dir / "analysis.json").is_file()
            or any(item.get("analysis_path") for item in submissions)
        )
        crashed = any(item.get("triggered") is True for item in submissions)
        if crashed:
            status = "success"
        elif stop_reason_hit and stop_reason_hit.startswith("iteration_cap:"):
            status = "iteration_cap"
        elif analysis_produced and timed_out:
            status = "iteration_cap"
        elif analysis_produced and returncode == 0:
            status = "agent_finished"
        else:
            status = "incomplete"

        poc_deduplication, deduplicated_pocs = deduplicate_submission_attempts(
            submissions
        )
        dsh_session_files = [str(path.relative_to(dsh_home)) for path in sorted(new_session_files)]
        trajectory_usage = summarize_dsh_sessions(new_session_files)
        completed_steps = trajectory_usage["completed_steps"]
        (run_dir / "trajectory").write_text(
            json.dumps(
                {
                    "backend": "deepseek_harness",
                    "profile": "headless",
                    "stdout": "dsh_stdout.txt",
                    "stderr": "dsh_stderr.txt",
                    "dsh_session_files": dsh_session_files,
                    "returncode": returncode,
                    "timed_out": timed_out,
                    "stop_reason": stop_reason_hit,
                    "completed_steps": completed_steps,
                    "usage": trajectory_usage,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        manifest = {
            "evaluation_protocol": "poc_analysis_artifact_per_submission_v3_dsh_local",
            "sample_id": args.sample_id,
            "harness": "deepseek_harness",
            "workspace_adapter": adapter_metadata,
            "dsh_profile": "headless",
            "model": args.model,
            "api_key_env": args.api_key_env,
            "base_url_configured": bool(args.base_url),
            "max_iter": args.max_iter,
            "iteration_cap": {
                "unit": "dsh_step_end",
                "limit": args.max_iter,
                "completed": completed_steps,
            },
            "harness_budget": trajectory_usage,
            "timeout": args.timeout,
            "tool_network": network_guard_manifest,
            "public_testcase_scrub": public_testcase_scrub,
            "runtime_readiness": runtime_readiness,
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "stop_reason": stop_reason_hit,
            "seconds": round(seconds, 1),
            "num_submission_attempts": len(submission_dirs),
            "submission_attempts": submissions,
            "poc_deduplication": poc_deduplication,
            "deduplicated_pocs": deduplicated_pocs,
            "analysis": {
                "produced": analysis_produced,
                "source": (
                    "last_valid_poc_submission"
                    if latest_analysis_exists
                    else final_analysis.get("source", "unknown")
                ),
                "path": "analysis.json",
                "format": "JSON object with sample_id, fine_trace, and vuln_logic",
            },
            "checkpoint": {
                "dir": "checkpoint/",
                "phase": "terminal",
                "contains_dsh_home": True,
            },
        }
        persist_results(
            sample_result_dir, workspace, run_dir, config_path, prompt_path, manifest
        )
        copy_dsh_checkpoint(dsh_home, sample_result_dir, new_session_files)
        slim_dsh_checkpoint_if_analysis_valid(sample_result_dir)
        reachability_metadata = run_reachability_pipeline(
            model_namespace=sample_result_dir.parent.name,
            sample_id=args.sample_id,
            sample_result_dir=sample_result_dir,
            enabled=args.run_reachability_after_generation,
            timeout=args.reachability_timeout,
            debugger_image=args.reachability_debugger_image,
            max_hits_per_event=args.reachability_max_hits_per_event,
            concurrency=args.reachability_concurrency,
            lock_dir=args.reachability_lock_dir.expanduser().resolve(),
        )
        print(f"[*] {args.sample_id}: reachability pipeline {reachability_metadata}")
        print(json.dumps(manifest, indent=2))
        return (
            0
            if status in {"success", "iteration_cap", "agent_finished"}
            and analysis_produced
            else 1
        )
    except Exception as exc:  # noqa: BLE001
        new_session_files: set[Path] = set()
        try:
            current_sessions = list_dsh_session_files(dsh_home)
            new_session_files = filter_dsh_session_files_for_workspace(
                current_sessions - preexisting_sessions, workspace
            )
            if not new_session_files and current_sessions:
                new_session_files = filter_dsh_session_files_for_workspace(
                    current_sessions, workspace
                )
            copy_dsh_checkpoint(dsh_home, sample_result_dir, new_session_files)
        except Exception:
            new_session_files = set()
        checkpoint_files = {}
        for name, path in (
            ("prompt.txt", prompt_path),
            ("dsh_config.json", config_path),
            ("args.json", run_dir / "args.json"),
            ("trajectory", run_dir / "trajectory"),
            ("dsh_stdout.txt", run_dir / "dsh_stdout.txt"),
            ("dsh_stderr.txt", run_dir / "dsh_stderr.txt"),
        ):
            if path.is_file():
                checkpoint_files[name] = path
        manifest = write_failure_artifact(
            sample_result_dir,
            sample_id=args.sample_id,
            harness="deepseek_harness",
            model=args.model,
            framework=framework,
            evaluation_protocol="poc_analysis_artifact_per_submission_v3_dsh_local",
            status="error",
            stop_reason="runner_exception",
            error=f"{type(exc).__name__}: {exc}",
            checkpoint_files=checkpoint_files,
            extra={
                "gt_sample_dir": str(gt_sample_dir),
                "scratch": str(scratch),
                "workspace": str(workspace),
                "workspace_adapter": adapter_metadata,
                "runtime_readiness": runtime_readiness,
                "public_testcase_scrub": public_testcase_scrub,
                "dsh_session_files": [
                    str(path.relative_to(dsh_home))
                    for path in sorted(new_session_files)
                    if dsh_home in path.parents
                ],
            },
        )
        print(json.dumps(manifest, indent=2, default=str))
        return 1
    finally:
        cleanup_dsh_scratch(scratch, scratch_root)


if __name__ == "__main__":
    raise SystemExit(main())
