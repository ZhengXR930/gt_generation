#!/usr/bin/env python3
"""Run baseline PoC-generation evaluations through one of the four adapters."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from poc_generation.adapters import HARNESSES, Request, build_command

LOG_ROOT = REPO_ROOT / "poc_generation" / "poc_results" / "_batch_logs"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _selected_valid_gt_samples(selector: str) -> list[str]:
    if selector not in {"valid_gt", "valid_gt_arvo", "valid_gt_non_arvo"}:
        raise ValueError(
            "unknown sample_selector "
            f"{selector!r}; expected valid_gt, valid_gt_arvo, or valid_gt_non_arvo"
        )
    payload = _load_json(REPO_ROOT / "gt_results" / "valid_gt.json")
    samples = [
        str(item).strip()
        for item in payload.get("samples", [])
        if str(item).strip()
    ]
    if selector == "valid_gt_arvo":
        samples = [sample for sample in samples if sample.startswith("arvo_")]
    elif selector == "valid_gt_non_arvo":
        samples = [sample for sample in samples if not sample.startswith("arvo_")]
    return samples


def _config_value(args: argparse.Namespace, config: dict[str, Any], name: str, default: Any = None) -> Any:
    value = getattr(args, name, None)
    if value not in (None, "", 0):
        return value
    return config.get(name, default)


def load_samples(args: argparse.Namespace, config: dict[str, Any]) -> list[str]:
    samples: list[str] = []
    selector = _config_value(args, config, "sample_selector", "")
    if selector:
        samples.extend(_selected_valid_gt_samples(str(selector)))
    samples.extend(str(item).strip() for item in config.get("samples", []) if str(item).strip())
    samples.extend(str(item).strip() for item in getattr(args, "sample", []) if str(item).strip())
    samples_file = _config_value(args, config, "samples_file", "")
    if samples_file:
        path = Path(samples_file).expanduser().resolve()
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("["):
            samples.extend(str(item).strip() for item in json.loads(text) if str(item).strip())
        else:
            samples.extend(line.strip() for line in text.splitlines() if line.strip())
    start = int(getattr(args, "start_index", 0) or 0)
    limit = int(getattr(args, "limit", 0) or 0)
    selected = list(dict.fromkeys(samples))[start:]
    if limit > 0:
        selected = selected[:limit]
    if not selected:
        raise ValueError("no samples selected")
    return selected


def result_is_complete(sample_dir: Path) -> bool:
    manifest = sample_dir / "manifest.json"
    analysis = sample_dir / "analysis.json"
    checkpoint = sample_dir / "checkpoint"
    if not manifest.is_file() or not analysis.is_file() or not checkpoint.is_dir():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        artifact = json.loads(analysis.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("status") not in {"success", "iteration_cap", "agent_finished"}:
        return False
    return (
        isinstance(artifact, dict)
        and artifact.get("sample_id") == sample_dir.name
        and isinstance(artifact.get("fine_trace"), list)
        and isinstance(artifact.get("vuln_logic"), dict)
    )


def maybe_run_reachability(config: dict[str, Any], *, namespace: str, sample_id: str, sample_dir: Path) -> dict[str, Any]:
    if not config.get("run_reachability_after_generation", True):
        return {"status": "disabled"}
    try:
        from evaluator.reachability.eval_batch import evaluate_model_sample

        result = evaluate_model_sample(
            model=namespace,
            sample_id=sample_id,
            sample_dir=sample_dir,
            timeout=int(config.get("reachability_timeout", 420)),
            debugger_image=str(config.get("reachability_debugger_image") or "gt-memory-env:latest"),
            max_hits_per_event=int(config.get("reachability_max_hits_per_event", 64)),
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    if "skipped" in result:
        return {"status": "skipped", "reason": result["skipped"]}
    if "error" in result:
        return {"status": "error", "error": result["error"]}
    return {"status": "complete", "summary": result.get("summary") or {}}


def build_request(args: argparse.Namespace, config: dict[str, Any], sample_id: str) -> Request:
    model = str(_config_value(args, config, "model"))
    namespace = str(
        _config_value(args, config, "namespace")
        or config.get("results_namespace")
        or model
    )
    return Request(
        harness=str(_config_value(args, config, "harness", "openhands")),
        sample_id=sample_id,
        model=model,
        namespace=namespace,
        base_url=str(_config_value(args, config, "base_url", "")),
        api_key_env=str(_config_value(args, config, "api_key_env", "")),
        api_version=str(_config_value(args, config, "api_version", "")),
        max_iter=int(_config_value(args, config, "max_iter", 100)),
        max_attempts=int(config.get("generation_attempts") or config.get("max_attempts") or args.max_attempts),
        timeout=int(_config_value(args, config, "timeout", 10800)),
        server=str(_config_value(args, config, "server", "http://host.docker.internal:8666")),
        difficulty=str(_config_value(args, config, "difficulty", "level1")),
        openhands_repo=Path(config["openhands_repo"]).expanduser() if config.get("openhands_repo") else None,
        extra_args=tuple(str(item) for item in config.get("extra_args", [])),
    )


def run_one(args: argparse.Namespace, config: dict[str, Any], sample_id: str) -> dict[str, Any]:
    request = build_request(args, config, sample_id)
    command = build_command(request)
    sample_dir = request.results_dir / sample_id
    if sample_dir.exists() and not args.overwrite and result_is_complete(sample_dir):
        record: dict[str, Any] = {"sample": sample_id, "status": "skipped", "results_dir": str(sample_dir)}
        if not (sample_dir / "reachability_eval.json").is_file():
            record["reachability"] = maybe_run_reachability(
                config, namespace=request.namespace, sample_id=sample_id, sample_dir=sample_dir
            )
        return record
    if args.dry_run:
        return {"sample": sample_id, "status": "planned", "command": command.redacted()}

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_dir = LOG_ROOT / request.namespace
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{sample_id}.log"
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("COMMAND " + json.dumps(command.redacted(), ensure_ascii=False) + "\n")
        log.flush()
        proc = subprocess.run(
            command.command,
            cwd=command.cwd,
            env=dict(command.env),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    record = {
        "sample": sample_id,
        "status": "complete" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "results_dir": str(sample_dir),
        "log": str(log_path),
    }
    if proc.returncode == 0:
        record["reachability"] = maybe_run_reachability(
            config, namespace=request.namespace, sample_id=sample_id, sample_dir=sample_dir
        )
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--samples-file")
    parser.add_argument("--sample-selector")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--harness", choices=HARNESSES)
    parser.add_argument("--model")
    parser.add_argument("--namespace")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env")
    parser.add_argument("--api-version")
    parser.add_argument("--max-iter", type=int)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--server")
    parser.add_argument("--difficulty")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = _load_json(args.config.expanduser().resolve()) if args.config else {}
    samples = load_samples(args, config)
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, int(args.parallel))) as executor:
        futures = {executor.submit(run_one, args, config, sample): sample for sample in samples}
        for future in as_completed(futures):
            record = future.result()
            counts[record["status"]] = counts.get(record["status"], 0) + 1
            print(json.dumps(record, ensure_ascii=False), flush=True)
    print(json.dumps({"final_counts": counts}, ensure_ascii=False), flush=True)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
