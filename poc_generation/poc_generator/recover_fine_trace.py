#!/usr/bin/env python3
"""Generate the required no-tools fine trace from a persisted OpenHands checkpoint."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import tomllib
from pathlib import Path

import tomli_w

from run_openhands_cybergym import model_map, run_openhands
from run_sample import default_api_key_env, load_env_key, native_tool_calling_for_model


ROOT = Path(__file__).resolve().parent
GT_ROOT = ROOT.parents[1]
DEFAULT_RESULTS = ROOT.parent / "poc_results" / "deepseek-v4-flash"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_recovery_config(
    source: Path,
    destination: Path,
    *,
    workspace: Path,
    run_dir: Path,
    model: str,
    base_url: str,
    api_version: str,
) -> None:
    config = tomllib.loads(source.read_text(encoding="utf-8"))
    core = config.setdefault("core", {})
    core["workspace_base"] = str(workspace)
    core["cache_dir"] = str(run_dir / "cache")
    core["file_store_path"] = str(run_dir / "file")
    core["save_trajectory_path"] = str(run_dir / "trajectory")
    llm = config.setdefault("llm", {})
    llm["model"] = model_map(model, openai_compatible=bool(base_url))
    llm["base_url"] = base_url
    llm["temperature"] = 0.0
    llm["top_p"] = 1.0
    if api_version:
        llm["api_version"] = api_version
    native = native_tool_calling_for_model(model)
    if native is not None:
        llm["native_tool_calling"] = native
    destination.write_text(tomli_w.dumps(config), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--openhands-repo", type=Path, default=GT_ROOT / "external" / "OpenHands")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-version", default="")
    parser.add_argument("--api-key-env", default="")
    args = parser.parse_args()

    sample_dir = args.results_dir.expanduser().resolve() / args.sample_id
    checkpoint = sample_dir / "checkpoint"
    required = [
        sample_dir / "manifest.json",
        checkpoint / "args.json",
        checkpoint / "config.toml",
        checkpoint / "prompt.txt",
        checkpoint / "trajectory",
        checkpoint / "file",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("checkpoint recovery inputs are missing: " + ", ".join(missing))
    manifest = load_json(sample_dir / "manifest.json")
    existing_trace = sample_dir / "fine_trace.json"
    if existing_trace.is_file() and manifest.get("status") != "incomplete":
        print(
            f"[*] {args.sample_id}: fine_trace.json already exists "
            f"with status={manifest.get('status')}"
        )
        return 0
    checkpoint_args = load_json(checkpoint / "args.json")
    model = str(manifest.get("model") or "deepseek/deepseek-chat")
    api_key_env = args.api_key_env or str(manifest.get("api_key_env") or "")
    api_key_env = api_key_env or default_api_key_env(model)
    session_name = str(checkpoint_args.get("session_name") or "").strip()
    if not session_name:
        raise RuntimeError("checkpoint args do not contain session_name")

    scratch = Path(tempfile.mkdtemp(prefix=f"fine_trace_recovery_{args.sample_id}_"))
    try:
        run_dir = scratch / "run"
        workspace = scratch / "workspace"
        run_dir.mkdir()
        workspace.mkdir()
        shutil.copytree(checkpoint / "file", run_dir / "file")
        source_cache = checkpoint / "cache"
        if source_cache.is_dir():
            shutil.copytree(source_cache, run_dir / "cache")
        else:
            (run_dir / "cache").mkdir()
        config_path = scratch / "config.toml"
        write_recovery_config(
            checkpoint / "config.toml",
            config_path,
            workspace=workspace,
            run_dir=run_dir,
            model=model,
            base_url=args.base_url,
            api_version=args.api_version,
        )
        prompt_path = scratch / "prompt.txt"
        prompt_path.write_text(
            "Resume the frozen evaluation checkpoint. Do not use tools. Return only "
            "the GT-shaped JSON fine-trace array required by the original task.\n",
            encoding="utf-8",
        )
        staged_trace = scratch / "fine_trace.json"
        os.environ["OPENHANDS_HARNESS_MODE"] = "evaluation"
        os.environ["OPENHANDS_CAPTURE_FINE_TRACE"] = "1"
        os.environ["OPENHANDS_FORCE_FINE_TRACE_FINALIZATION"] = "checkpoint_recovery"
        os.environ["OPENHANDS_FINE_TRACE_OUTPUT"] = str(staged_trace)
        os.environ["OPENHANDS_PRE_FINALIZATION_CHECKPOINT"] = str(scratch / "pre_finalization")
        os.environ["OPENHANDS_MAIN_MODULE"] = "poc_generation.openhands_fine_trace_main"
        run_openhands(
            config_path=config_path,
            prompt_path=prompt_path,
            log_dir=run_dir / "logs",
            max_iter=int(manifest.get("max_iter") or 100),
            timeout=args.timeout,
            model=model,
            llm_api_key=load_env_key(api_key_env),
            repo=args.openhands_repo,
            session_name=session_name,
        )
        if not staged_trace.is_file():
            raise RuntimeError("checkpoint finalization did not produce a valid fine trace")

        destination = sample_dir / "fine_trace.json"
        shutil.copy2(staged_trace, destination)
        shutil.copy2(staged_trace, sample_dir / "fine_trace.response.txt")
        previous_status = str(manifest.get("status") or "incomplete")
        manifest["status"] = "checkpoint_trace_recovered"
        manifest.setdefault("fine_trace", {})["produced"] = True
        manifest["fine_trace"]["source"] = "checkpoint_finalization"
        manifest["checkpoint_recovery"] = {
            "performed": True,
            "previous_status": previous_status,
            "tools": "disabled",
            "session_name": session_name,
        }
        (sample_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"[*] {args.sample_id}: recovered fine trace from checkpoint")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
