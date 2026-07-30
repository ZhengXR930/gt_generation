#!/usr/bin/env python3
"""Rerun the configured 100-sample DeepSeek and GPT PoC evaluation batches.

Results are replaced in-place by run_sample.py.  A sample is skipped only when
its current model-specific result is already a complete v2, 100-iteration run.
"""

import fcntl
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


HERE = Path(__file__).resolve().parent
RESULTS_ROOT = HERE.parent / "poc_results"
LOG_ROOT = RESULTS_ROOT / "_batch_logs"
LOCK_ROOT = RESULTS_ROOT / "_sample_locks"
RUN_SAMPLE = HERE / "run_sample.py"


def load_config(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def result_is_complete(result_dir: Path) -> bool:
    manifest_path = result_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("evaluation_protocol") != "poc_trace_per_submission_v2":
        return False
    if manifest.get("max_iter") != 100:
        return False
    if manifest.get("status") not in {"success", "iteration_cap", "agent_finished"}:
        return False
    if not (result_dir / "fine_trace.json").is_file():
        return False
    if not (result_dir / "checkpoint").is_dir():
        return False
    deduplication = manifest.get("poc_deduplication") or {}
    deduplicated_pocs = manifest.get("deduplicated_pocs")
    if not isinstance(deduplicated_pocs, list):
        return False
    if deduplication.get("total_poc_submissions") != len(
        manifest.get("submission_attempts") or []
    ):
        return False
    if deduplication.get("deduplicated_poc_count") != len(deduplicated_pocs):
        return False
    for attempt in manifest.get("submission_attempts", []):
        attempt_id = str(attempt.get("attempt_id") or "")
        attempt_dir = result_dir / "submissions" / attempt_id
        if not attempt_id or not attempt_dir.is_dir():
            return False
        required = {
            "poc.bin",
            "candidate_trace.json",
            "candidate_trace.response.txt",
            "request.json",
            "result.json",
            "runtime_output.txt",
        }
        if not all((attempt_dir / name).is_file() for name in required):
            return False
    for poc in deduplicated_pocs:
        for key in (
            "representative_trace_path",
            "representative_poc_path",
            "representative_runtime_output_path",
        ):
            relative_path = poc.get(key)
            if not relative_path or not (result_dir / relative_path).is_file():
                return False
    return True


def run_one(config: dict, sample_id: str) -> dict:
    namespace = config["results_namespace"]
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_ROOT / f"{sample_id}.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"model": namespace, "sample": sample_id, "status": "deferred"}
        result_dir = RESULTS_ROOT / namespace / sample_id
        if result_is_complete(result_dir):
            return {"model": namespace, "sample": sample_id, "status": "skipped"}

        log_dir = LOG_ROOT / namespace
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{sample_id}.log"
        command = [
            sys.executable,
            str(RUN_SAMPLE),
            "--arvo-id",
            sample_id.removeprefix("arvo_"),
            "--max-iter",
            str(config["max_iter"]),
            "--server",
            config["server"],
            "--difficulty",
            config["difficulty"],
            "--model",
            config["model"],
            "--openhands-repo",
            config["openhands_repo"],
            "--base-url",
            config.get("base_url", ""),
            "--api-key-env",
            config["api_key_env"],
            "--results-dir",
            str(RESULTS_ROOT / namespace),
            "--max-attempts",
            str(config.get("max_attempts", 1)),
        ]
        started = time.monotonic()
        with log_path.open("w", encoding="utf-8") as log:
            returncode = subprocess.run(
                command,
                cwd=HERE.parents[1],
                stdout=log,
                stderr=subprocess.STDOUT,
            ).returncode
    return {
        "model": namespace,
        "sample": sample_id,
        "status": "complete" if returncode == 0 else "failed",
        "returncode": returncode,
        "seconds": round(time.monotonic() - started, 1),
        "log": str(log_path),
    }


def main() -> int:
    deepseek_first = load_config("poc_config.deepseek_50.json")
    deepseek_second = load_config("poc_config.deepseek_additional50.json")
    gpt = load_config("poc_config.gpt54_mini_additional100.json")

    deepseek = dict(deepseek_first)
    deepseek["samples"] = list(
        dict.fromkeys(deepseek_first["samples"] + deepseek_second["samples"])
    )
    jobs = []
    for deepseek_sample, gpt_sample in zip(deepseek["samples"], gpt["samples"]):
        jobs.append((deepseek, deepseek_sample))
        jobs.append((gpt, gpt_sample))

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    summary_path = LOG_ROOT / "rerun_100_models.jsonl"
    counts = {"complete": 0, "failed": 0, "skipped": 0}
    with summary_path.open("a", encoding="utf-8") as summary:
        # Two total concurrent agents keeps the two models progressing without
        # multiplying the resource use of the unrelated GT generation batch.
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(run_one, config, sample): (config, sample)
                for config, sample in jobs
            }
            for future in as_completed(futures):
                config, sample = futures[future]
                try:
                    record = future.result()
                except Exception as exc:
                    record = {
                        "model": config["results_namespace"],
                        "sample": sample,
                        "status": "failed",
                        "error": repr(exc),
                    }
                counts[record["status"]] += 1
                line = json.dumps(record, ensure_ascii=False)
                print(line, flush=True)
                summary.write(line + "\n")
                summary.flush()
    print(json.dumps({"final_counts": counts}), flush=True)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
