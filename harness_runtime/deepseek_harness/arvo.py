#!/usr/bin/env python3
"""DeepSeek Harness runner for ARVO/CyberGym PoC generation samples.

This runner executes DeepSeek Harness on the host.  It uses CyberGym only for
task materialization and submission validation; the subject agent sees the same
public ARVO task workspace shape as other harnesses, but no OpenHands checkout is
loaded.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

DSH_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = DSH_ROOT.parent
GT_ROOT = RUNTIME_ROOT.parent

sys.path.insert(0, str(RUNTIME_ROOT))
sys.path.insert(0, str(GT_ROOT))
sys.path.insert(0, str(GT_ROOT / "external" / "cybergym" / "src"))

from harness_runtime.python_env import ensure_repo_python  # noqa: E402

ensure_repo_python(GT_ROOT, min_version=(3, 11))

from harness_runtime.analysis_artifact import analysis_artifact_schema_instructions  # noqa: E402
from harness_runtime.submission_db import NOT_CRASHED, check as check_success  # noqa: E402
from cybergym.task.gen_task import generate_task  # noqa: E402
from cybergym.task.types import TaskConfig, TaskDifficulty  # noqa: E402
from harness_runtime.dedup import deduplicate_submission_attempts  # noqa: E402
from harness_runtime.failure_artifact import write_failure_artifact  # noqa: E402
from harness_runtime.deepseek_harness.local import (  # noqa: E402
    DEFAULT_DSH_COMMAND,
    DEFAULT_DSH_HOME,
    DEFAULT_DSH_NODE_ROOT,
    DEFAULT_DSH_SCRATCH_ROOT,
    cleanup_dsh_scratch,
    compile_network_guard,
    copy_dsh_checkpoint,
    create_network_guard_bin,
    count_dsh_completed_steps,
    filter_dsh_session_files_for_workspace,
    list_dsh_session_files,
    network_guard_allowed_hosts,
    persist_final_stdout_analysis,
    run_dsh,
    scrub_agent_visible_public_testcases,
    slim_dsh_checkpoint_if_analysis_valid,
    summarize_dsh_sessions,
    write_dsh_settings,
)
from harness_runtime.auth import default_api_key_env, load_env_key  # noqa: E402
from harness_runtime.openhands.arvo import (  # noqa: E402
    clear_previous_result,
    ensure_arvo_source,
    materialize_attempt_analysis_files,
)
from harness_runtime.deepseek_harness.reachability import (  # noqa: E402
    DEFAULT_REACHABILITY_LOCK_DIR,
    run_reachability_pipeline,
)
from harness_runtime.workspace import (  # noqa: E402
    install_submit_candidate_guard,
    render_prompt,
    run_workspace_installer,
)


def arvo_host_server(server: str) -> str:
    """DSH runs on the host, so use a host-reachable CyberGym endpoint."""
    value = server.strip() or "http://127.0.0.1:8666"
    return value.replace("host.docker.internal", "127.0.0.1")


def cleanup_arvo_target_image(arvo_id: str) -> dict:
    """Remove this sample's ARVO target image and stopped containers.

    ARVO uses one large `n132/arvo:<id>-vul` image per sample.  Keeping every
    image after a batch quickly exhausts disk.  This cleanup runs only after the
    sample attempt has fully persisted submissions/checkpoint, so it does not
    affect the current validation path.
    """
    image = f"n132/arvo:{arvo_id}-vul"
    result: dict = {"image": image, "containers_removed": [], "image_removed": False}
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"ancestor={image}", "--format", "{{.ID}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if ps.returncode == 0:
        for container_id in [line.strip() for line in ps.stdout.splitlines() if line.strip()]:
            rm = subprocess.run(
                ["docker", "rm", "-f", container_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if rm.returncode == 0:
                result["containers_removed"].append(container_id)
            else:
                result.setdefault("container_errors", []).append(
                    {"container": container_id, "stderr": rm.stderr[-1000:]}
                )
    else:
        result["container_list_error"] = ps.stderr[-1000:]
    rmi = subprocess.run(
        ["docker", "image", "rm", "-f", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if rmi.returncode == 0:
        result["image_removed"] = True
    else:
        result["image_remove_error"] = rmi.stderr[-1000:]
    return result


def adapt_arvo_workspace_for_host(workspace: Path, sample_id: str) -> None:
    """Patch generated ARVO paths from Docker /workspace to host DSH cwd."""
    workspace_text = str(workspace)
    path = workspace / "submit.sh"
    text = path.read_text(encoding="utf-8")
    placeholder = "__GT_GENERATION_DSH_WORKSPACE__"
    text = text.replace("/workspace/", f"{placeholder}/")
    text = text.replace("/workspace", placeholder)
    text = text.replace(placeholder, workspace_text)
    path.write_text(text, encoding="utf-8")

    submit_path = workspace / "submit.sh"
    install_submit_candidate_guard(submit_path)
    text = submit_path.read_text(encoding="utf-8")
    marker_match = re.search(
        r"printf '%s\\n' \"\$ATTEMPT_ID\" > .*/\.poc_submission_recorded\n",
        text,
    )
    if marker_match is None:
        raise RuntimeError("generated submit.sh changed unexpectedly; cannot install DSH submission ledger")
    marker = marker_match.group(0)
    submission_root = shlex.quote(str(workspace / ".submissions"))
    ledger_insert = (
        f"SUBMISSION_ROOT={submission_root}\n"
        'SUBMISSION_DIR="${SUBMISSION_ROOT}/${ATTEMPT_ID}"\n'
        'mkdir -p "$SUBMISSION_DIR"\n'
        'cp "$POC_FILE" "$SUBMISSION_DIR/poc.bin" 2>/dev/null || true\n'
        'cp "$ANALYSIS_FILE" "$SUBMISSION_DIR/analysis.json" 2>/dev/null || true\n'
        'cp "$RESPONSE_FILE" "$SUBMISSION_DIR/response.json" 2>/dev/null || true\n'
        'cp "$TRACE_FILE" "$SUBMISSION_DIR/candidate_trace.json" 2>/dev/null || true\n'
        'chmod -R a+rwX "$SUBMISSION_DIR"\n'
    )
    # Persist the exact submitted artifacts before the normal success marker.
    # The runner's terminal-condition poller waits for this marker before
    # stopping the harness, so a successful server-side crash cannot kill DSH
    # before analysis.json is copied locally.
    text = text[: marker_match.start()] + ledger_insert + marker + text[marker_match.end() :]
    invalid_trace_match = re.search(
        r'if \[ "\$TRACE_VALID" != "true" \]; then\n',
        text,
    )
    if invalid_trace_match is not None and "DSH_TRACE_INVALID_LEDGER" not in text:
        invalid_trace_ledger = (
            'if [ "$TRACE_VALID" != "true" ]; then\n'
            '    DSH_TRACE_INVALID_LEDGER=1\n'
            f"    SUBMISSION_ROOT={submission_root}\n"
            '    SUBMISSION_DIR="${SUBMISSION_ROOT}/${ATTEMPT_ID:-trace-invalid}"\n'
            '    mkdir -p "$SUBMISSION_DIR"\n'
            '    cp "$POC_FILE" "$SUBMISSION_DIR/poc.bin" 2>/dev/null || true\n'
            '    cp "$ANALYSIS_FILE" "$SUBMISSION_DIR/analysis.json" 2>/dev/null || true\n'
            '    cp "$RESPONSE_FILE" "$SUBMISSION_DIR/response.json" 2>/dev/null || true\n'
            '    cp "$TRACE_FILE" "$SUBMISSION_DIR/candidate_trace.json" 2>/dev/null || true\n'
            '    chmod -R a+rwX "$SUBMISSION_DIR"\n'
        )
        text = (
            text[: invalid_trace_match.start()]
            + invalid_trace_ledger
            + text[invalid_trace_match.end() :]
        )
    submit_path.write_text(text, encoding="utf-8")
    submit_path.chmod(0o755)


def prepare_arvo_workspace(
    *, arvo_id: str, scratch: Path, server: str, difficulty: str
) -> tuple[Path, dict]:
    os.environ["CYBERGYM_PREEXTRACT_REPO_TAR"] = "1"
    ensure_arvo_source(arvo_id)
    task_id = f"arvo:{arvo_id}"
    workspace = scratch / "workspace"
    workspace.mkdir(parents=True)
    task = generate_task(
        TaskConfig(
            task_id=task_id,
            out_dir=workspace,
            data_dir=GT_ROOT / "external" / "cybergym_data_subset" / "data",
            server=server,
            difficulty=TaskDifficulty(difficulty),
            agent_id=uuid.uuid4().hex,
        )
    )
    sample_id = f"arvo_{arvo_id}"
    adapt_arvo_workspace_for_host(workspace, sample_id)
    task_dump = task.model_dump()
    task_dump["public_testcase_scrub"] = scrub_agent_visible_public_testcases(workspace)
    return workspace, task_dump


def persist_arvo_submission_attempts(
    *,
    sample_dir: Path,
    workspace: Path,
    cybergym_agent_id: str,
    attempts: list[dict],
    fallback_analysis: Path | None,
    server_root: Path,
) -> list[dict]:
    source_root = server_root / "logs" / "submissions" / cybergym_agent_id
    destination_root = sample_dir / "submissions"
    destination_root.mkdir(parents=True, exist_ok=True)
    persisted: list[dict] = []
    for sequence, attempt in enumerate(attempts, 1):
        attempt_id = str(attempt.get("attempt_id") or "")
        if not attempt_id:
            continue
        source = source_root / attempt_id
        destination = destination_root / attempt_id
        if source.is_dir() and not destination.exists():
            shutil.copytree(source, destination)
        destination.mkdir(parents=True, exist_ok=True)

        workspace_attempt = workspace / ".submissions" / attempt_id
        workspace_analysis = workspace_attempt / "analysis.json"
        if workspace_analysis.is_file():
            shutil.copy2(workspace_analysis, destination / "analysis.json")
        elif fallback_analysis is not None and fallback_analysis.is_file():
            shutil.copy2(fallback_analysis, destination / "analysis.json")
        workspace_response = workspace_attempt / "response.json"
        if workspace_response.is_file():
            shutil.copy2(workspace_response, destination / "response.json")

        analysis_valid = materialize_attempt_analysis_files(destination)
        record = dict(attempt)
        vul_exit_code = record.get("vul_exit_code")
        record.update(
            {
                "sequence_in_run": sequence,
                "result_path": f"submissions/{attempt_id}/",
                "analysis_valid": analysis_valid,
                "analysis_path": (
                    f"submissions/{attempt_id}/analysis.json"
                    if (destination / "analysis.json").is_file()
                    else None
                ),
                "poc_path": (
                    f"submissions/{attempt_id}/poc.bin"
                    if (destination / "poc.bin").is_file()
                    else None
                ),
                "runtime_output_path": (
                    f"submissions/{attempt_id}/runtime_output.txt"
                    if (destination / "runtime_output.txt").is_file()
                    else None
                ),
                "triggered": (
                    vul_exit_code is not None and vul_exit_code not in NOT_CRASHED
                ),
            }
        )
        persisted.append(record)
    return persisted


def run_attempt(args: argparse.Namespace, sample_result_dir: Path, attempt: int) -> str:
    arvo_id = args.arvo_id
    sample_id = f"arvo_{arvo_id}"
    scratch_root = args.scratch_root.expanduser().resolve()
    framework = (
        "reward_framework"
        if os.getenv("REWARD_FRAMEWORK_RUN_ID")
        else "poc_generation"
    )
    started = time.monotonic()
    try:
        scratch_root.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix=f"run_dsh_arvo_{sample_id}_", dir=scratch_root))
    except Exception as exc:  # noqa: BLE001
        manifest = write_failure_artifact(
            sample_result_dir,
            sample_id=sample_id,
            harness="deepseek_harness",
            model=args.model,
            framework=framework,
            evaluation_protocol="poc_analysis_artifact_per_submission_v3_dsh_arvo",
            status="error",
            stop_reason="scratch_unavailable",
            error=f"{type(exc).__name__}: {exc}",
            seconds=round(time.monotonic() - started, 3),
            extra={
                "arvo_id": arvo_id,
                "task_id": f"arvo:{arvo_id}",
                "attempt": attempt,
                "scratch_root": str(scratch_root),
            },
            overwrite_manifest=True,
        )
        print(json.dumps(manifest, indent=2, default=str))
        return "error"
    workspace = scratch / "workspace"
    run_dir = scratch / "results" / f"{sample_id}-{uuid.uuid4().hex}"
    dsh_home = args.dsh_home.expanduser().resolve()
    prompt_path = scratch / "prompt.txt"
    config_path = scratch / "dsh_config.json"
    preexisting_sessions: set[Path] = set()
    task: dict = {"agent_id": None}
    adapter_metadata = None
    network_guard_manifest: dict = {"mode": "not_initialized"}
    try:
        workspace, task = prepare_arvo_workspace(
            arvo_id=arvo_id,
            scratch=scratch,
            server=arvo_host_server(args.server),
            difficulty=args.difficulty,
        )
        adapter_metadata = run_workspace_installer(
            args.workspace_installer,
            harness="deepseek_harness",
            workspace=workspace,
            sample_id=sample_id,
            scratch=scratch,
            env=os.environ,
        )
        run_dir.mkdir(parents=True)
        for name in ("file", "cache"):
            (run_dir / name).mkdir()

        preexisting_sessions = list_dsh_session_files(dsh_home)
        write_dsh_settings(dsh_home, args.model, args.reasoning_effort)

        prompt_path.write_text(
            render_prompt(args.prompt_file, sample_id=sample_id, workspace=workspace),
            encoding="utf-8",
        )
        config_payload = {
            "harness": "deepseek_harness",
            "backend": "arvo_cybergym",
            "profile": "headless",
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "base_url_configured": bool(args.base_url),
            "api_version_ignored": bool(args.api_version),
            "dsh_src": str(args.dsh_src.expanduser().resolve()),
            "node_root": str(args.node_root.expanduser().resolve()),
            "dsh_command": args.dsh_command,
            "dsh_home": str(dsh_home),
            "checkpoint_policy": "copy_settings_profiles_and_new_sessions_only",
            "workspace_adapter": adapter_metadata,
        }
        config_path.write_text(json.dumps(config_payload, indent=2), encoding="utf-8")
        (run_dir / "args.json").write_text(
            json.dumps(
                {
                    "agent": f"deepseek-harness:{args.model}",
                    "sample_id": sample_id,
                    "task": task,
                    "attempt": attempt,
                    "workspace": str(workspace),
                    "dsh_home": str(dsh_home),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        network_guard_bin = None
        network_guard_manifest = {"mode": "allowed"}
        if not args.allow_tool_network:
            guard_so = compile_network_guard()
            allowed_hosts = network_guard_allowed_hosts(arvo_host_server(args.server))
            network_guard_bin = create_network_guard_bin(scratch, guard_so, allowed_hosts)
            network_guard_manifest = {
                "mode": "blocked_except_local_cybergym_server",
                "guard_so": str(guard_so),
                "allowed_hosts": allowed_hosts,
                "blocked": [
                    "external IPv4/IPv6 connect/sendto",
                    "docker/containerd unix sockets",
                    "docker CLI via PATH wrapper",
                ],
            }

        db_path = args.server_root / "poc.db"

        def stop_after_terminal_condition() -> str | None:
            success = (
                db_path.exists()
                and check_success(db_path, task["agent_id"]).get("success")
            )
            if success and (workspace / ".poc_submission_recorded").is_file():
                return "successful_submission"
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
            dsh_command=args.dsh_command,
            dsh_home=dsh_home,
            workspace=workspace,
            prompt_path=prompt_path,
            run_dir=run_dir,
            api_key=load_env_key(args.api_key_env),
            base_url=args.base_url,
            timeout=args.timeout,
            network_guard_bin=network_guard_bin,
            stop_when=stop_after_terminal_condition,
            stop_poll_seconds=1.0,
        )

        success_info = (
            check_success(db_path, task["agent_id"])
            if db_path.exists()
            else {
                "ok": False,
                "error": "db not found",
                "submissions": [],
                "submission_attempts": [],
                "success": False,
            }
        )
        current_sessions = list_dsh_session_files(dsh_home)
        new_session_files = filter_dsh_session_files_for_workspace(
            current_sessions - preexisting_sessions, workspace
        )
        if not new_session_files and current_sessions:
            new_session_files = filter_dsh_session_files_for_workspace(
                current_sessions, workspace
            )

        final_analysis = persist_final_stdout_analysis(
            run_dir / "dsh_stdout.txt", sample_result_dir, sample_id
        )
        latest_analysis = workspace / ".latest_analysis.json"
        persisted_attempts = persist_arvo_submission_attempts(
            sample_dir=sample_result_dir,
            workspace=workspace,
            cybergym_agent_id=task["agent_id"],
            attempts=success_info.get("submission_attempts") or [],
            fallback_analysis=latest_analysis if latest_analysis.is_file() else None,
            server_root=args.server_root,
        )
        if latest_analysis.is_file():
            try:
                raw = latest_analysis.read_text(encoding="utf-8")
                value = json.loads(raw)
                if value.get("sample_id") == sample_id:
                    sample_result_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(latest_analysis, sample_result_dir / "analysis.json")
            except (OSError, TypeError, json.JSONDecodeError):
                pass

        analysis_produced = (
            final_analysis.get("produced") is True
            or latest_analysis.is_file()
            or (sample_result_dir / "analysis.json").is_file()
            or any(item.get("analysis_path") for item in persisted_attempts)
        )
        crashed = bool(success_info.get("success"))
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
            persisted_attempts
        )
        for item in deduplicated_pocs:
            attempt_id = str(item.get("representative_attempt_id") or "").strip()
            if attempt_id:
                item["representative_result_path"] = f"submissions/{attempt_id}"
        dsh_session_files = [
            str(path.relative_to(dsh_home)) for path in sorted(new_session_files)
        ]
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
            "evaluation_protocol": "poc_analysis_artifact_per_submission_v3_dsh_arvo",
            "arvo_id": arvo_id,
            "task_id": f"arvo:{arvo_id}",
            "sample_id": sample_id,
            "cybergym_agent_id": task["agent_id"],
            "model": args.model,
            "harness": "deepseek_harness",
            "workspace_adapter": adapter_metadata,
            "dsh_profile": "headless",
            "api_key_env": args.api_key_env,
            "base_url_configured": bool(args.base_url),
            "api_version_ignored": bool(args.api_version),
            "max_iter": args.max_iter,
            "iteration_cap": {
                "unit": "dsh_step_end",
                "limit": args.max_iter,
                "completed": completed_steps,
            },
            "harness_budget": trajectory_usage,
            "timeout": args.timeout,
            "tool_network": network_guard_manifest,
            "public_testcase_scrub": task.get("public_testcase_scrub"),
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "stop_reason": stop_reason_hit,
            "seconds": round(seconds, 1),
            "poc_generation": success_info,
            "num_submission_attempts": len(persisted_attempts),
            "submission_attempts": persisted_attempts,
            "poc_deduplication": poc_deduplication,
            "deduplicated_pocs": deduplicated_pocs,
            "analysis": {
                "produced": analysis_produced,
                "source": (
                    "last_valid_poc_submission"
                    if latest_analysis.is_file()
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

        from harness_runtime.openhands.local import persist_results

        persist_results(sample_result_dir, workspace, run_dir, config_path, prompt_path, manifest)
        copy_dsh_checkpoint(dsh_home, sample_result_dir, new_session_files)
        slim_dsh_checkpoint_if_analysis_valid(sample_result_dir)
        reachability_metadata = run_reachability_pipeline(
            model_namespace=sample_result_dir.parent.name,
            sample_id=sample_id,
            sample_result_dir=sample_result_dir,
            enabled=args.run_reachability_after_generation,
            timeout=args.reachability_timeout,
            debugger_image=args.reachability_debugger_image,
            max_hits_per_event=args.reachability_max_hits_per_event,
            concurrency=args.reachability_concurrency,
            lock_dir=args.reachability_lock_dir.expanduser().resolve(),
        )
        print(f"[*] {sample_id}: reachability pipeline {reachability_metadata}")
        print(json.dumps(manifest, indent=2))
        return status
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
        agent_id = task.get("agent_id") if isinstance(task, dict) else None
        manifest = write_failure_artifact(
            sample_result_dir,
            sample_id=sample_id,
            harness="deepseek_harness",
            model=args.model,
            framework=framework,
            evaluation_protocol="poc_analysis_artifact_per_submission_v3_dsh_arvo",
            status="error",
            stop_reason="runner_exception",
            error=f"{type(exc).__name__}: {exc}",
            seconds=round(time.monotonic() - started, 3),
            checkpoint_files=checkpoint_files,
            extra={
                "arvo_id": arvo_id,
                "task_id": f"arvo:{arvo_id}",
                "cybergym_agent_id": agent_id,
                "attempt": attempt,
                "scratch": str(scratch),
                "workspace": str(workspace),
                "workspace_adapter": adapter_metadata,
                "tool_network": network_guard_manifest,
                "dsh_session_files": [
                    str(path.relative_to(dsh_home))
                    for path in sorted(new_session_files)
                    if dsh_home in path.parents
                ],
            },
        )
        print(json.dumps(manifest, indent=2, default=str))
        return "error"
    finally:
        if getattr(args, "cleanup_target_image", False):
            try:
                cleanup_result = cleanup_arvo_target_image(arvo_id)
                cleanup_path = sample_result_dir / "target_image_cleanup.json"
                cleanup_path.write_text(
                    json.dumps(cleanup_result, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                print(f"[*] {sample_id}: target image cleanup {cleanup_result}")
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask run status.
                print(f"[!] {sample_id}: target image cleanup failed: {type(exc).__name__}: {exc}")
        cleanup_dsh_scratch(scratch, scratch_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arvo-id", required=True)
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--server", default="http://host.docker.internal:8666")
    parser.add_argument("--difficulty", default="level1")
    parser.add_argument("--timeout", type=int, default=10800)
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
        "--dsh-command",
        default=DEFAULT_DSH_COMMAND,
        help="DeepSeek Harness CLI command, e.g. dsh or npx -y @deepseek-ai/dsh.",
    )
    parser.add_argument(
        "--dsh-home",
        type=Path,
        default=DEFAULT_DSH_HOME,
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
    )
    parser.add_argument("--allow-tool-network", action="store_true")
    parser.add_argument("--cleanup-target-image", action="store_true")
    parser.add_argument(
        "--run-reachability-after-generation",
        action="store_true",
        default=True,
        help="Run per-sample reachability immediately after PoC generation and before target image cleanup.",
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
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--workspace-installer", default="")
    args = parser.parse_args()
    args.prompt_file = args.prompt_file.expanduser().resolve()
    args.server_root = args.server_root.expanduser().resolve()
    if not args.prompt_file.is_file():
        parser.error(f"prompt file not found: {args.prompt_file}")

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    results_dir = args.results_dir.expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    sample_id = f"arvo_{args.arvo_id}"
    sample_result_dir = results_dir / sample_id
    sample_result_dir.mkdir(parents=True, exist_ok=True)

    api_key_env = args.api_key_env or default_api_key_env(args.model)
    args.api_key_env = api_key_env
    last_status = None
    for attempt in range(1, args.max_attempts + 1):
        clear_previous_result(sample_result_dir)
        print(f"[*] {sample_id}: DSH generation attempt {attempt}/{args.max_attempts}")
        last_status = run_attempt(args, sample_result_dir, attempt)
        analysis_path = sample_result_dir / "analysis.json"
        manifest_path = sample_result_dir / "manifest.json"
        has_submission_analysis = False
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for item in manifest.get("submission_attempts") or []:
                    relative = item.get("analysis_path")
                    if relative and (sample_result_dir / str(relative)).is_file():
                        has_submission_analysis = True
                        break
            except (OSError, TypeError, json.JSONDecodeError):
                has_submission_analysis = False
        if (
            last_status in {"success", "iteration_cap", "agent_finished"}
            and (analysis_path.is_file() or has_submission_analysis)
        ):
            print(
                f"[*] {sample_id}: clean DSH endpoint on attempt {attempt} "
                f"(status={last_status}); "
                f"{'final analysis artifact captured' if analysis_path.is_file() else 'per-submission analysis captured'}"
            )
            return 0
        print(
            f"[*] {sample_id}: attempt {attempt} did not yield a clean analysis "
            f"artifact (status={last_status}); "
            f"{'retrying' if attempt < args.max_attempts else 'giving up'}"
        )
    print(
        f"[!] {sample_id}: no analysis artifact after {args.max_attempts} DSH "
        f"attempts (last status={last_status})"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
