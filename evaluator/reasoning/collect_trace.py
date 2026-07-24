#!/usr/bin/env python3
"""Reasoning trace collector: resume a sample's frozen PoC-generation checkpoint
on a THROWAWAY copy, freeze exploration, disable all tools, and have the coding
agent lay out the vulnerability's logic chain as a fine-trace JSON. Scoring is
separate (scoring.py: trace vs verified invariants) -- there are no probing
questions.

Checkpoint layout under poc_generation/poc_results/<sample_id>/checkpoint/:
    file/          OpenHands FileStore (agent_state.pkl + events/)
    trajectory     full action/observation history JSON
    cache/         LLM completion cache
    config.toml    resume config (paths get rewritten into the scratch copy)
    args.json      run metadata, including "session_name"

The original checkpoint is never touched: everything runs against a
tempfile.mkdtemp() copy that is ALWAYS deleted afterward (the finally block),
so the same checkpoint can be re-collected later without drift and the copies
never accumulate on disk. The persisted checkpoint excludes the sandbox
workspace/ (the extracted repo can be 1-2GB and is not needed once tools are
off) -- an empty workspace dir is created in the copy just so the runtime has
something to mount.

Writes poc_generation/poc_results/<sample_id>/reasoning_trace.json.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]     # evaluator/reasoning/ -> repo root
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"


def load_env_key(var_name: str) -> str:
    if os.environ.get(var_name):
        return os.environ[var_name]
    for line in (REPO_ROOT / "config.txt").read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{var_name}="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError(f"{var_name} not found")


def make_scratch_copy(sample_dir: Path) -> Path:
    checkpoint = sample_dir / "checkpoint"
    scratch = Path(tempfile.mkdtemp(prefix=f"trace_{sample_dir.name}_"))
    (scratch / "results").mkdir()
    (scratch / "workspace").mkdir()

    for name in ("file", "cache"):
        src = checkpoint / name
        if src.exists():
            shutil.copytree(src, scratch / "results" / name)
        else:
            (scratch / "results" / name).mkdir()
    for name in ("trajectory", "args.json"):
        src = checkpoint / name
        if src.exists():
            shutil.copy2(src, scratch / "results" / name)

    config_text = (checkpoint / "config.toml").read_text()
    config_text = _rewrite_toml_path(config_text, "workspace_base", str(scratch / "workspace"))
    config_text = _rewrite_toml_path(config_text, "cache_dir", str(scratch / "results" / "cache"))
    config_text = _rewrite_toml_path(config_text, "file_store_path", str(scratch / "results" / "file"))
    config_text = _rewrite_toml_path(config_text, "save_trajectory_path", str(scratch / "results" / "trajectory"))
    (scratch / "config.toml").write_text(config_text)
    return scratch


def _rewrite_toml_path(text: str, key: str, new_value: str) -> str:
    import re

    pattern = re.compile(rf'^{key}\s*=\s*".*"$', re.MULTILINE)
    replacement = f'{key} = "{new_value}"'
    if not pattern.search(text):
        raise ValueError(f"config.toml missing key: {key}")
    return pattern.sub(replacement, text)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample-id", required=True, help="e.g. arvo_1304")
    ap.add_argument("--max-iter", type=int, default=100)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--keep-scratch", action="store_true", help="Keep the scratch copy (debugging only).")
    args = ap.parse_args()

    sample_dir = POC_RESULTS / args.sample_id
    checkpoint = sample_dir / "checkpoint"
    if not checkpoint.exists():
        sys.exit(f"No checkpoint at {checkpoint}")

    session_name = json.loads((checkpoint / "args.json").read_text())["session_name"]
    poetry_path = shutil.which("poetry")
    if not poetry_path:
        sys.exit("poetry not found on PATH")

    scratch = make_scratch_copy(sample_dir)
    try:
        prompt_path = scratch / "resume_prompt.txt"
        prompt_path.write_text(
            "Resuming after the checkpoint. Continue if there is remaining budget."
        )
        trace_output = scratch / "reasoning_trace.json"

        env = dict(os.environ)
        env["PYTHONPATH"] = f"{REPO_ROOT}:{REPO_ROOT / 'external/cybergym/src'}" + (
            f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
        )
        env["LLM_API_KEY"] = load_env_key("DEEPSEEK_API_KEY")
        env["OPENHANDS_HARNESS_MODE"] = "evaluation"
        # Freeze-and-ask, in trace mode: the harness disables tools and asks for
        # the vulnerability logic-chain JSON (see reasoning/session.py's trace
        # prompt). No probing_questions.json is involved.
        env["OPENHANDS_EVAL_PROBING"] = "1"
        env["OPENHANDS_EVAL_TRACE_MODE"] = "1"
        env["OPENHANDS_EVAL_PROBE_OUTPUT"] = str(trace_output)
        env["LOG_TO_FILE"] = "1"
        env["LOG_DIR"] = str(scratch / "logs")
        env["LOG_ALL_EVENTS"] = "1"

        cmd = [
            poetry_path, "run", "python", "-m", "openhands.core.main",
            "--config-file", str(scratch / "config.toml"),
            "--file", str(prompt_path),
            "--max-iterations", str(args.max_iter),
            "--name", session_name,
        ]  # fmt: skip

        print(f"[*] Collecting trace for '{args.sample_id}' (session '{session_name}') via scratch {scratch}")
        subprocess.run(cmd, cwd=REPO_ROOT / "external/OpenHands", env=env, timeout=args.timeout)

        if trace_output.exists():
            dest = sample_dir / "reasoning_trace.json"
            shutil.copy2(trace_output, dest)
            print(f"\n[*] Wrote {dest}")
        else:
            print("\n[!] reasoning_trace.json was not produced -- the trace prompt likely did not fire")
    finally:
        # Always drop the throwaway copy so the original checkpoint is untouched
        # and copies never pile up on disk (they include the full FileStore).
        if args.keep_scratch:
            print(f"[*] Keeping scratch copy at {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
