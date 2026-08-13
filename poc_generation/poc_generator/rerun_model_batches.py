#!/usr/bin/env python3
"""Rerun the configured 100-sample DeepSeek and GPT PoC evaluation batches.

Results are replaced in-place by run_sample.py.  A sample is skipped only when
its current model-specific result is already a complete v2, 100-iteration run.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


HERE = Path(__file__).resolve().parent
GT_ROOT = HERE.parents[1]
for import_root in (GT_ROOT, GT_ROOT / "evaluator"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))
RESULTS_ROOT = HERE.parent / "poc_results"
LOG_ROOT = RESULTS_ROOT / "_batch_logs"
LOCK_ROOT = RESULTS_ROOT / "_sample_locks"
RUN_SAMPLE = HERE / "run_sample.py"
RUN_LOCAL_SAMPLE = HERE / "run_local_sample.py"


def sample_environment(config: dict) -> dict[str, str]:
    env = os.environ.copy()
    runtime_network = str(config.get("openhands_runtime_docker_network") or "").strip()
    if runtime_network:
        env["OPENHANDS_RUNTIME_DOCKER_NETWORK"] = runtime_network
    runtime_extra_hosts = str(config.get("openhands_runtime_extra_hosts") or "").strip()
    if runtime_extra_hosts:
        env["OPENHANDS_RUNTIME_EXTRA_HOSTS"] = runtime_extra_hosts
    if config.get("openhands_runtime_disable_dns"):
        env["OPENHANDS_RUNTIME_DISABLE_DNS"] = "1"
    return env


def maybe_run_reachability(config: dict, namespace: str, sample_id: str) -> dict:
    if not config.get("run_reachability_after_generation"):
        return {"status": "disabled"}
    try:
        from evaluator.reachability.eval_batch import evaluate_model_sample

        result = evaluate_model_sample(
            model=namespace,
            sample_id=sample_id,
            sample_dir=RESULTS_ROOT / namespace / sample_id,
            timeout=int(config.get("reachability_timeout", 420)),
            debugger_image=str(config.get("reachability_debugger_image") or "gt-memory-env:latest"),
            max_hits_per_event=int(config.get("reachability_max_hits_per_event", 64)),
        )
    except Exception as exc:  # noqa: BLE001 - do not fail PoC generation.
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if "skipped" in result:
        return {"status": "skipped", "reason": result["skipped"]}
    if "error" in result:
        return {"status": "error", "error": result["error"]}
    summary = result.get("summary") or {}
    return {
        "status": "complete",
        "submitted_unique_pocs": summary.get("submitted_unique_pocs"),
        "evaluated_unique_pocs": summary.get("evaluated_unique_pocs"),
        "gt_triggered_pocs": summary.get("gt_triggered_pocs"),
        "any_reached": summary.get("any_reached"),
        "any_target_vulnerability_triggered": summary.get(
            "any_target_vulnerability_triggered"
        ),
    }


def remove_prefix(value: str, prefix: str) -> str:
    return value[len(prefix):] if value.startswith(prefix) else value


def cleanup_arvo_assets(sample_id: str) -> dict:
    """Remove rebuildable per-sample source and Docker assets after a run."""
    if not re.fullmatch(r"arvo_\d+", sample_id):
        return {"status": "skipped", "reason": "not_arvo"}

    arvo_id = remove_prefix(sample_id, "arvo_")
    data_root = (
        HERE.parents[1] / "external" / "cybergym_data_subset" / "data" / "arvo"
    ).resolve()
    sample_root = (data_root / arvo_id).resolve()
    if sample_root.parent != data_root or sample_root.name != arvo_id:
        return {"status": "refused", "reason": "unexpected_sample_path"}

    image_name = f"n132/arvo:{arvo_id}-vul"
    removed_containers = []
    image_status = "absent"
    try:
        import docker

        client = docker.from_env()
        containers = client.containers.list(all=True, filters={"ancestor": image_name})
        protected = [
            container.name
            for container in containers
            if container.status in {"running", "restarting", "paused"}
        ]
        if protected:
            return {
                "status": "skipped",
                "reason": "active_target_container",
                "containers": protected,
            }
        for container in containers:
            container.remove(force=container.status == "created")
            removed_containers.append(container.name)
        try:
            client.images.remove(image_name, force=False)
            image_status = "removed"
        except docker.errors.ImageNotFound:
            pass
    except Exception as exc:
        image_status = f"retained: {type(exc).__name__}: {exc}"

    repo_dir = sample_root / "repo-vul"
    repo_status = "absent"
    try:
        if repo_dir.is_symlink():
            repo_dir.unlink()
            repo_status = "symlink_removed"
        elif repo_dir.is_dir():
            shutil.rmtree(repo_dir)
            repo_status = "removed"
        elif repo_dir.exists():
            repo_status = "refused_non_directory"
    except Exception as exc:
        repo_status = f"retained: {type(exc).__name__}: {exc}"

    return {
        "status": "complete",
        "repo": repo_status,
        "image": image_status,
        "removed_containers": removed_containers,
    }


def load_config(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


COMPLETE_STATUSES = {
    "success",
    "iteration_cap",
    "agent_finished",
    "iteration_cap_trace_recovered",
    "agent_finished_trace_recovered",
    "checkpoint_trace_recovered",
}


def has_valid_analysis(result_dir: Path) -> bool:
    analysis_path = result_dir / "analysis.json"
    if not analysis_path.is_file():
        return False
    try:
        payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and set(payload) == {"sample_id", "fine_trace", "vuln_logic"}
        and payload.get("sample_id") == result_dir.name
    )


def submission_files_complete(result_dir: Path, manifest: dict, *, local: bool) -> bool:
    # Historical v2 result packages often preserved only poc.bin plus the
    # per-submission analysis artifact; reachability replays PoCs later and
    # regenerates runtime evidence, so those older packages are still valid for
    # OpenHands coverage as long as the top-level analysis/checkpoint is usable.
    protocol = manifest.get("evaluation_protocol")
    if protocol in {"poc_trace_per_submission_v2", "poc_trace_per_submission_v2_local"}:
        return True

    attempts = manifest.get("submission_attempts") or []
    for attempt in attempts:
        attempt_id = str(attempt.get("attempt_id") or "")
        attempt_dir = result_dir / "submissions" / attempt_id
        if not attempt_id or not attempt_dir.is_dir():
            return False
        required = {
            "poc.bin",
            "analysis.json",
            "result.json",
            "runtime_output.txt",
        }
        if not local:
            required.add("request.json")
        if not all((attempt_dir / name).is_file() for name in required):
            return False

    deduplicated_pocs = manifest.get("deduplicated_pocs")
    if not isinstance(deduplicated_pocs, list):
        return True
    deduplication = manifest.get("poc_deduplication") or {}
    total = deduplication.get("total_poc_submissions")
    deduped = deduplication.get("deduplicated_poc_count")
    if total is not None and total != len(attempts):
        return False
    if deduped is not None and deduped != len(deduplicated_pocs):
        return False
    for poc in deduplicated_pocs:
        for key in (
            "representative_analysis_path",
            "representative_poc_path",
            "representative_runtime_output_path",
        ):
            relative_path = poc.get(key)
            if not relative_path or not (result_dir / relative_path).is_file():
                return False
    return True


def result_is_complete(result_dir: Path) -> bool:
    manifest_path = result_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("max_iter") != 100:
        return False
    if manifest.get("status") not in COMPLETE_STATUSES:
        return False
    if not has_valid_analysis(result_dir):
        return False
    if not (result_dir / "checkpoint").is_dir():
        return False
    return submission_files_complete(result_dir, manifest, local=False)


def local_result_is_complete(result_dir: Path) -> bool:
    manifest_path = result_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("max_iter") != 100:
        return False
    if manifest.get("status") not in COMPLETE_STATUSES:
        return False
    if not has_valid_analysis(result_dir):
        return False
    if not (result_dir / "checkpoint").is_dir():
        return False
    return submission_files_complete(result_dir, manifest, local=True)


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
        is_arvo = sample_id.startswith("arvo_")
        complete = result_is_complete(result_dir) if is_arvo else local_result_is_complete(result_dir)
        if complete:
            record = {"model": namespace, "sample": sample_id, "status": "skipped"}
            if not (result_dir / "reachability_eval.json").is_file():
                record["reachability"] = maybe_run_reachability(
                    config, namespace, sample_id
                )
            if is_arvo and config.get("cleanup_arvo_assets"):
                record["cleanup"] = cleanup_arvo_assets(sample_id)
            return record

        log_dir = LOG_ROOT / namespace
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{sample_id}.log"
        if is_arvo:
            command = [
                sys.executable, str(RUN_SAMPLE), "--arvo-id",
                remove_prefix(sample_id, "arvo_"), "--max-iter",
                str(config["max_iter"]), "--server", config["server"],
                "--difficulty", config["difficulty"], "--model", config["model"],
                "--openhands-repo", config["openhands_repo"], "--base-url",
                config.get("base_url", ""), "--api-key-env", config["api_key_env"],
                "--results-dir", str(RESULTS_ROOT / namespace), "--max-attempts",
                str(config.get("max_attempts", 1)),
            ]
            command.extend(
                ["--harness-profile", str(config.get("harness_profile") or "baseline")]
            )
        else:
            command = [
                sys.executable, str(RUN_LOCAL_SAMPLE), "--sample-id", sample_id,
                "--max-iter", str(config["max_iter"]), "--timeout",
                str(config.get("timeout", 7200)), "--model", config["model"],
                "--openhands-repo", config["openhands_repo"], "--base-url",
                config.get("base_url", ""), "--api-key-env", config["api_key_env"],
                "--results-dir", str(RESULTS_ROOT / namespace),
            ]
        api_version = str(config.get("api_version") or "").strip()
        if api_version:
            command.extend(["--api-version", api_version])
        started = time.monotonic()
        with log_path.open("w", encoding="utf-8") as log:
            returncode = subprocess.run(
                command,
                cwd=HERE.parents[1],
                env=sample_environment(config),
                stdout=log,
                stderr=subprocess.STDOUT,
            ).returncode
    record = {
        "model": namespace,
        "sample": sample_id,
        "status": "complete" if returncode == 0 else "failed",
        "returncode": returncode,
        "seconds": round(time.monotonic() - started, 1),
        "log": str(log_path),
    }
    if returncode == 0:
        record["reachability"] = maybe_run_reachability(
            config, namespace, sample_id
        )
    if is_arvo and config.get("cleanup_arvo_assets"):
        record["cleanup"] = cleanup_arvo_assets(sample_id)
    return record


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
