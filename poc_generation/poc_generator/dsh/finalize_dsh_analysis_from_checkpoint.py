#!/usr/bin/env python3
"""Finalize missing DSH analysis.json artifacts from timed-out checkpoints.

This is a finalization-only recovery path for runs that reached the step cap
without a PoC submission and without a final analysis artifact.  It does not
submit PoCs or run reachability; it asks the same DSH model to convert the
saved trajectory plus issue/source context into the required analysis.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
GENERATOR_ROOT = HERE.parent
GT_ROOT = GENERATOR_ROOT.parents[1]
RESULTS_ROOT = GENERATOR_ROOT.parent / "poc_results"

sys.path.insert(0, str(GENERATOR_ROOT))
sys.path.insert(0, str(GT_ROOT))

from evaluator.reasoning.analysis_artifact import validate_analysis_artifact_quality  # noqa: E402
from dsh.run_deepseek_harness_local_sample import (  # noqa: E402
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
    summarize_dsh_sessions,
    write_dsh_settings,
)
from openhands_backend.run_sample import load_env_key  # noqa: E402


SCHEMA = """Required analysis.json schema:
- Return/write one bare JSON object with exactly: sample_id, fine_trace, vuln_logic.
- fine_trace is an ordered vulnerable-implementation-source causal path. A
  harness/test/fuzz frame may appear only as an unscored intermediate when
  needed to show how bytes enter the target; it must not be role="source",
  role="root_cause", role="sink", or a vuln_logic propagation endpoint. Each
  step must contain:
  step:int, file:string, function:string, line:int|null, var:string, code:string,
  note:string, and role:"source"|"root_cause"|"sink"|"intermediate"|null.
  Do not output depends_on.
- Mark exactly one fine_trace step role="source", exactly one role="root_cause",
  and exactly one role="sink".
- vuln_logic must be an object with source, root_cause, sink, propagation, and
  optional issue_alignment.
- vuln_logic.source/root_cause/sink copy file/function/line from the matching
  role-marked fine_trace step and include operands: non-empty string array.
  In vuln_logic, line must be an integer.
- root_cause and sink must include relation exactly {"op": "...", "left": "...",
  "right": "..."}. op must be one of eq, ne, lt, le, gt, ge, same_object.
- propagation is an array of edges. Each edge contains from, to, type, via, and
  optional relation. from/to copy file/function/line from existing fine_trace
  steps and each from/to endpoint must include operands: non-empty string array.
  type is data, control, or order. via is a non-empty string array.
- Use vulnerable implementation source locations only for source/root_cause/sink
  and vuln_logic propagation endpoints. Do not cite README, description.txt,
  analysis files, checkpoint files, runtime logs, harness/test/fuzz setup,
  build/setup wrappers, or old results as scored anchors. If input first appears
  only in a harness, keep that step intermediate and choose the first downstream
  vulnerable implementation statement as source.
"""


def failed_samples_from_summary(summary_path: Path) -> list[str]:
    samples: list[str] = []
    for line in summary_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "failed":
            sample = str(record.get("sample") or "")
            if sample:
                samples.append(sample)
    return samples


def checkpoint_excerpt(sample_dir: Path, *, max_messages: int = 18, max_chars: int = 24000) -> str:
    session_files = sorted((sample_dir / "checkpoint" / "dsh_home" / "sessions-jsonl").glob("**/session.jsonl"))
    messages: list[tuple[int, str]] = []
    for session_file in session_files:
        for line in session_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "assistant/message":
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            texts: list[str] = []
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"text", "reasoning"}:
                    text = str(block.get("text") or "").strip()
                    if text:
                        texts.append(text)
            joined = "\n".join(texts).strip()
            if joined:
                messages.append((int(data.get("step") or 0), joined))
    selected = messages[-max_messages:]
    rendered = "\n\n".join(
        f"## checkpoint assistant step {step}\n{text}" for step, text in selected
    )
    if len(rendered) > max_chars:
        rendered = rendered[-max_chars:]
    return rendered


def write_workspace(sample_id: str, scratch: Path, sample_dir: Path) -> tuple[Path, dict[str, Any]]:
    arvo_id = sample_id.split("_", 1)[1]
    source_root = GT_ROOT / "external" / "cybergym_data_subset" / "data" / "arvo" / arvo_id
    workspace = scratch / "workspace"
    workspace.mkdir(parents=True)
    repo_vul = workspace / "repo-vul"
    repo_vul.symlink_to(source_root / "repo-vul", target_is_directory=True)
    description = (source_root / "description.txt").read_text(encoding="utf-8", errors="replace")
    excerpt = checkpoint_excerpt(sample_dir)
    (workspace / "README.md").write_text(
        "# Finalize timed-out PoC-generation analysis\n\n"
        "This workspace is for finalization only. Do not build, run, submit, or create a PoC.\n\n"
        "## Public issue description\n\n"
        f"{description.strip()}\n\n"
        "## Saved checkpoint trajectory excerpt\n\n"
        f"{excerpt.strip()}\n\n"
        f"{SCHEMA}\n",
        encoding="utf-8",
    )
    scrub = scrub_agent_visible_public_testcases(workspace)
    return workspace, {
        "arvo_id": arvo_id,
        "source_root": str(source_root),
        "public_testcase_scrub": scrub,
    }


def prompt(sample_id: str, max_steps: int) -> str:
    return f"""You are finalizing a timed-out PoC generation checkpoint for {sample_id}.

Read README.md and, if necessary, inspect repo-vul/src-vul for cited project
source lines. Do not build, run, submit, create, or modify any PoC. Do not use
public testcases. Your only task is to output the best current structured
analysis artifact for the timed-out subject-agent trajectory.

Use the issue description as trusted task context. Use the checkpoint excerpt as
the subject agent's saved reasoning state. If the checkpoint was uncertain,
choose the most concrete source/root/sink hypothesis that is supported by the
issue and source code.

Finish within {max_steps} DSH steps. Your final answer must be one bare JSON
object and nothing else.

{SCHEMA}
"""


def finalize_one(args: argparse.Namespace, sample: str) -> dict[str, Any]:
    result_root = RESULTS_ROOT / args.namespace
    sample_dir = result_root / sample
    if (sample_dir / "analysis.json").is_file() and not args.force:
        return {"sample": sample, "status": "skipped_existing_analysis"}
    scratch_root = args.scratch_root.expanduser().resolve()
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"run_dsh_finalize_{sample}_", dir=scratch_root))
    try:
        workspace, workspace_meta = write_workspace(sample, scratch, sample_dir)
        run_dir = scratch / "results" / f"{sample}-finalize-{uuid.uuid4().hex}"
        run_dir.mkdir(parents=True)
        for name in ("file", "cache"):
            (run_dir / name).mkdir()
        dsh_home = args.dsh_home.expanduser().resolve()
        preexisting_sessions = list_dsh_session_files(dsh_home)
        write_dsh_settings(dsh_home, args.model, args.reasoning_effort)
        prompt_path = scratch / "prompt.txt"
        prompt_path.write_text(prompt(sample, args.max_finalize_steps), encoding="utf-8")
        config_path = scratch / "dsh_finalize_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "harness": "deepseek_harness_finalize_analysis",
                    "model": args.model,
                    "reasoning_effort": args.reasoning_effort,
                    "dsh_home": str(dsh_home),
                    **workspace_meta,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        network_guard_bin = None
        network_manifest: dict[str, Any] = {"mode": "allowed"}
        if not args.allow_tool_network:
            guard_so = compile_network_guard()
            network_guard_bin = create_network_guard_bin(scratch, guard_so, ["127.0.0.1", "::1"])
            network_manifest = {
                "mode": "blocked_except_localhost",
                "guard_so": str(guard_so),
                "allowed_hosts": ["127.0.0.1", "::1"],
            }

        def stop_after_step_cap() -> str | None:
            session_files = filter_dsh_session_files_for_workspace(
                list_dsh_session_files(dsh_home), workspace
            )
            completed = count_dsh_completed_steps(session_files)
            if completed >= args.max_finalize_steps:
                return f"finalize_step_cap:{completed}"
            return None

        started = time.monotonic()
        returncode, timed_out, stop_reason, seconds = run_dsh(
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
            stop_when=stop_after_step_cap,
            stop_poll_seconds=1.0,
        )
        current_sessions = list_dsh_session_files(dsh_home)
        new_sessions = filter_dsh_session_files_for_workspace(
            current_sessions - preexisting_sessions, workspace
        )
        if not new_sessions:
            new_sessions = filter_dsh_session_files_for_workspace(current_sessions, workspace)
        recovery_dir = sample_dir / "checkpoint" / "analysis_finalization"
        if recovery_dir.exists():
            shutil.rmtree(recovery_dir)
        recovery_dir.mkdir(parents=True, exist_ok=True)
        for name in ("dsh_stdout.txt", "dsh_stderr.txt", "trajectory"):
            src = run_dir / name
            if src.is_file():
                shutil.copy2(src, recovery_dir / name)
        shutil.copy2(prompt_path, recovery_dir / "prompt.txt")
        shutil.copy2(config_path, recovery_dir / "config.json")
        copy_dsh_checkpoint(dsh_home, recovery_dir, new_sessions)

        final = persist_final_stdout_analysis(run_dir / "dsh_stdout.txt", sample_dir, sample)
        produced = bool(final.get("produced"))
        error = None
        if produced:
            raw = (sample_dir / "analysis.json").read_text(encoding="utf-8")
            error = validate_analysis_artifact_quality(raw)
            produced = error is None
        manifest_path = sample_dir / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["analysis_recovery"] = {
                "status": "recovered" if produced else "failed",
                "method": "dsh_checkpoint_finalization",
                "returncode": returncode,
                "timed_out": timed_out,
                "stop_reason": stop_reason,
                "seconds": round(seconds, 1),
                "max_finalize_steps": args.max_finalize_steps,
                "harness_budget": summarize_dsh_sessions(new_sessions),
                "tool_network": network_manifest,
                "validation_error": error,
                "started_elapsed_seconds": round(time.monotonic() - started, 1),
            }
            if produced:
                manifest["analysis"] = {
                    "produced": True,
                    "source": "checkpoint_finalization",
                    "path": "analysis.json",
                    "format": "JSON object with sample_id, fine_trace, and vuln_logic",
                }
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return {
            "sample": sample,
            "status": "recovered" if produced else "failed",
            "returncode": returncode,
            "timed_out": timed_out,
            "stop_reason": stop_reason,
            "seconds": round(seconds, 1),
            "validation_error": error,
        }
    finally:
        cleanup_dsh_scratch(scratch, scratch_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--max-finalize-steps", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--reasoning-effort", default="high", choices=("off", "high", "max"))
    parser.add_argument("--dsh-home", type=Path, default=Path("/home/xinran/.cache/gt_generation_deepseek_harness_home"))
    parser.add_argument("--scratch-root", type=Path, default=DEFAULT_DSH_SCRATCH_ROOT)
    parser.add_argument("--dsh-src", type=Path, default=GT_ROOT / "external" / "deepseek-harness")
    parser.add_argument("--node-root", type=Path, default=Path("/home/xinran/.local/node-v24-musl"))
    parser.add_argument("--allow-tool-network", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("samples", nargs="*")
    args = parser.parse_args()

    samples = list(args.samples)
    if args.summary is not None:
        samples.extend(failed_samples_from_summary(args.summary))
    samples = [sample for sample in dict.fromkeys(samples) if sample != "arvo_10999"]
    if args.parallel <= 1:
        for sample in samples:
            print(json.dumps(finalize_one(args, sample), ensure_ascii=False), flush=True)
        return 0
    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {executor.submit(finalize_one, args, sample): sample for sample in samples}
        for future in as_completed(futures):
            sample = futures[future]
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001
                record = {
                    "sample": sample,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            print(json.dumps(record, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
