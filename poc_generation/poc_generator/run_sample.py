#!/usr/bin/env python3
"""Drive one CyberGym+OpenHands PoC-generation run for a single GT_generation sample.

Runs the actual generation attempt (tools + real sandbox repo) in a throwaway
scratch dir, then copies only the durable checkpoint pieces (file_store,
trajectory, config.toml, args.json -- NOT the extracted repo/workspace, which
can be 1-2GB and isn't needed to resume/probe later) into
poc_generation/<sample_id>/checkpoint/, and writes poc_generation/<sample_id>/manifest.json.

Use build_probes.py + run_probe.py afterward to probe the resulting checkpoint.
"""
import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent            # poc_generation/poc_generator/
GT_ROOT = ROOT.parents[1]                          # repo root (external/, config.txt, poc_results/)
POC_RESULTS = ROOT.parent / "poc_results"          # per-sample checkpoint + manifest live here
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(GT_ROOT / "external" / "cybergym" / "src"))

from run_openhands_cybergym import run_with_configs, OpenhandsArgs, LLMArgs, TaskArgs  # noqa: E402
from cybergym.task.types import TaskDifficulty  # noqa: E402
from check_success import check as check_success  # noqa: E402


def load_env_key(var_name: str) -> str:
    if os.environ.get(var_name):
        return os.environ[var_name]
    cfg = GT_ROOT / "config.txt"
    for line in cfg.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{var_name}="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError(f"{var_name} not found in env or {cfg}")


def find_run_dir(log_dir: Path, task_id_safe: str, agent_id: str | None) -> Path | None:
    if agent_id:
        candidate = log_dir / f"{task_id_safe}-{agent_id}"
        if candidate.exists():
            return candidate
    matches = sorted(log_dir.glob(f"{task_id_safe}-*"), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def run_attempt(args, task_id: str, task_id_safe: str, sample_id: str) -> str | None:
    """One full generation episode on a fresh scratch + Docker workspace. Copies
    the durable checkpoint pieces and writes the manifest to poc_results/<id>/.
    Returns the run status -- 'success' (PoC crashed), 'iteration_cap' (reached
    the iteration limit), or 'incomplete' (died early, e.g. a stuck loop) -- or
    None if no run dir was produced. Always cleans up its scratch copy.

    Only 'success'/'iteration_cap' reach a clean freeze, so only those produce a
    reasoning trace; 'incomplete' means the episode should be re-run (see main)."""
    scratch = Path(tempfile.mkdtemp(prefix=f"run_{sample_id}_"))
    scratch_log_dir = scratch / "results"
    scratch_tmp_dir = scratch / "tmp"
    scratch_log_dir.mkdir()
    scratch_tmp_dir.mkdir()

    openhands_args = OpenhandsArgs(
        log_dir=scratch_log_dir,
        tmp_dir=scratch_tmp_dir,
        llm=LLMArgs(model=args.model),
        max_iter=args.max_iter,
        repo=GT_ROOT / "external" / "OpenHands",
        remove_tmp=False,  # need config.toml still present to copy it out below
        timeout=args.timeout,
    )
    task_args = TaskArgs(
        task_id=task_id,
        data_dir=GT_ROOT / "external" / "cybergym_data_subset" / "data",
        server=args.server,
        difficulty=TaskDifficulty(args.difficulty),
    )

    try:
        try:
            returned_agent_id = run_with_configs(openhands_args, task_args)
        except Exception as exc:
            logging.warning(f"run_with_configs raised {exc!r}; still attempting checkpoint save from partial state")
            returned_agent_id = None

        run_dir = find_run_dir(openhands_args.log_dir, task_id_safe, returned_agent_id)
        if run_dir is None:
            print(json.dumps({"arvo_id": args.arvo_id, "status": "no_run_dir_found"}, indent=2))
            return None

        args_json = json.loads((run_dir / "args.json").read_text())
        cybergym_agent_id = args_json["task"]["agent_id"]

        db_path = ROOT / "server" / "poc.db"
        success_info = (
            check_success(db_path, cybergym_agent_id) if db_path.exists() else {"ok": False, "error": "db not found"}
        )

        # A reasoning trace is written ONLY when the episode reaches a clean
        # freeze (PoC submitted / iteration limit / agent finished). Its
        # presence is therefore the reliable signal that the run terminated
        # cleanly -- unlike a trajectory-length heuristic, which a stuck loop
        # inflates past max_iter and so misreports an early death as a genuine
        # iteration cap. No trace + no success => the episode died early
        # (stuck loop / error) and should be re-run (see main).
        trace_produced = (POC_RESULTS / sample_id / "reasoning_trace.json").exists()
        if success_info.get("success"):
            status = "success"
        elif trace_produced:
            status = "iteration_cap"
        else:
            status = "incomplete"

        # Copy only the durable checkpoint pieces -- not the extracted repo/workspace.
        sample_dir = POC_RESULTS / sample_id
        checkpoint_dir = sample_dir / "checkpoint"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        tmp_input_dir = openhands_args.tmp_dir / run_dir.name

        for name in ("file", "cache"):
            src = run_dir / name
            dst = checkpoint_dir / name
            if dst.exists():
                shutil.rmtree(dst)
            if src.exists():
                shutil.copytree(src, dst)
            else:
                dst.mkdir()
        for name in ("trajectory", "args.json"):
            src = run_dir / name
            if src.exists():
                shutil.copy2(src, checkpoint_dir / name)
        for name in ("config.toml", "prompt.txt"):
            src = tmp_input_dir / "template" / name
            if src.exists():
                shutil.copy2(src, checkpoint_dir / name)

        manifest_entry = {
            "arvo_id": args.arvo_id,
            "task_id": task_id,
            "sample_id": sample_id,
            "session_name": args_json["session_name"],
            "cybergym_agent_id": cybergym_agent_id,
            "model": args.model,
            "max_iter": args.max_iter,
            "status": status,
            "poc_generation": success_info,
            "checkpoint": {
                "dir": "checkpoint/",
                "note": (
                    "workspace/ is intentionally NOT persisted here (the extracted repo "
                    "can be 1-2GB and isn't needed for tool-free probing). Re-materialize "
                    "from external/cybergym_data_subset/data/arvo/<id>/ if genuine "
                    "tool-using continuation is needed later."
                ),
            },
        }
        manifest_path = sample_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_entry, indent=2, default=str))
        print(json.dumps(manifest_entry, indent=2, default=str))
        print(f"\n[*] Wrote checkpoint to {checkpoint_dir}")
        print(f"[*] Wrote manifest to {manifest_path}")
        return status
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arvo-id", required=True, help="numeric ARVO id, e.g. 1304")
    ap.add_argument("--max-iter", type=int, default=100)
    ap.add_argument("--server", default="http://host.docker.internal:8666")
    ap.add_argument("--difficulty", default="level1")
    ap.add_argument("--timeout", type=int, default=10800)
    ap.add_argument("--model", default="deepseek/deepseek-chat")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="Re-run the whole episode up to this many times if it dies early "
                         "(stuck loop / no clean freeze), since only a clean freeze yields a trace.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    os.environ["DEEPSEEK_API_KEY"] = load_env_key("DEEPSEEK_API_KEY")
    # Ship the pre-extracted repo-vul/ directory instead of repo-vul.tar.gz.
    # Extracting a large tarball with `tar xzf` regularly takes >10s, which
    # trips OpenHands' "no output yet, wait or interrupt" nudge; deepseek-chat
    # then repeatedly retries a plain "C-c" (missing is_input=true, so it's
    # never actually delivered to the running process) until the identical
    # rejected-command loop trips the stuck-loop detector and kills the whole
    # episode a few steps in -- observed on both arvo_1304 and arvo_14467.
    # Pre-extracting removes the only long-running command early in the task.
    os.environ["CYBERGYM_PREEXTRACT_REPO_TAR"] = "1"

    task_id = f"arvo:{args.arvo_id}"
    task_id_safe = task_id.replace(":", "_")
    sample_id = f"arvo_{args.arvo_id}"

    # Collect the reasoning fine-trace in the SAME generation run: when
    # exploration freezes (PoC submitted / iteration limit), the harness asks
    # for the vulnerability logic-chain JSON (validated by a format hook) and
    # writes it here. No separate checkpoint-resume needed -- the trace is a
    # by-product of the generation episode. Saved alongside the checkpoint for
    # traceability; scored later by evaluator/reasoning/scoring.py.
    trace_output = POC_RESULTS / sample_id / "reasoning_trace.json"
    trace_output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["OPENHANDS_HARNESS_MODE"] = "evaluation"
    os.environ["OPENHANDS_EVAL_PROBING"] = "1"
    os.environ["OPENHANDS_EVAL_TRACE_MODE"] = "1"
    os.environ["OPENHANDS_EVAL_PROBE_OUTPUT"] = str(trace_output)

    last_status = None
    for attempt in range(1, args.max_attempts + 1):
        # A fresh episode each attempt: overwrites this sample's reasoning_trace.json
        # only when it reaches a clean freeze. Start clean so a stale trace from a
        # prior early-died attempt cannot be mistaken for this attempt's output.
        trace_output.unlink(missing_ok=True)
        print(f"[*] {sample_id}: generation attempt {attempt}/{args.max_attempts}")
        last_status = run_attempt(args, task_id, task_id_safe, sample_id)
        if last_status in ("success", "iteration_cap") and trace_output.exists():
            print(f"[*] {sample_id}: clean freeze on attempt {attempt} (status={last_status}); trace captured")
            return
        print(f"[*] {sample_id}: attempt {attempt} did not yield a trace "
              f"(status={last_status}); {'retrying' if attempt < args.max_attempts else 'giving up'}")
    print(f"[!] {sample_id}: no reasoning trace after {args.max_attempts} attempts (last status={last_status})")
    sys.exit(1)


if __name__ == "__main__":
    main()
