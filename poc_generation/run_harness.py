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

from harness_runtime.failure_artifact import write_failure_artifact
from model_router import resolve_model_route
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


def update_manifest_reachability(sample_dir: Path, reachability: dict[str, Any]) -> None:
    manifest_path = sample_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    manifest["reachability"] = reachability
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def build_request(args: argparse.Namespace, config: dict[str, Any], sample_id: str) -> Request:
    harness = str(_config_value(args, config, "harness", "openhands"))
    explicit_namespace = str(
        _config_value(args, config, "namespace")
        or config.get("results_namespace")
        or ""
    )
    route = resolve_model_route(
        surface="poc_generation",
        harness=harness,
        model_route=str(_config_value(args, config, "model_route", "")),
        model=str(_config_value(args, config, "model", "")),
        base_url=str(_config_value(args, config, "base_url", "")),
        api_key_env=str(_config_value(args, config, "api_key_env", "")),
        api_version=str(_config_value(args, config, "api_version", "")),
        namespace=explicit_namespace,
    )
    if not route.model:
        raise ValueError("model or model_route is required")
    extra_args = [str(item) for item in config.get("extra_args", [])]
    extra_args.extend(route.extra_args)
    return Request(
        harness=harness,
        sample_id=sample_id,
        model=route.model,
        namespace=route.results_namespace or route.model,
        base_url=route.base_url,
        api_key_env=route.api_key_env,
        api_version=route.api_version,
        max_iter=int(_config_value(args, config, "max_iter", 100)),
        max_attempts=int(config.get("generation_attempts") or config.get("max_attempts") or args.max_attempts),
        timeout=int(_config_value(args, config, "timeout", 10800)),
        server=str(_config_value(args, config, "server", "http://host.docker.internal:8666")),
        difficulty=str(_config_value(args, config, "difficulty", "level1")),
        openhands_repo=Path(config["openhands_repo"]).expanduser() if config.get("openhands_repo") else None,
        extra_args=tuple(extra_args),
    )


def run_one(args: argparse.Namespace, config: dict[str, Any], sample_id: str) -> dict[str, Any]:
    started = time.monotonic()
    request = build_request(args, config, sample_id)
    sample_dir = request.results_dir / sample_id
    if sample_dir.exists() and not args.overwrite and result_is_complete(sample_dir):
        record: dict[str, Any] = {"sample": sample_id, "status": "skipped", "results_dir": str(sample_dir)}
        if not (sample_dir / "reachability_eval.json").is_file():
            record["reachability"] = maybe_run_reachability(
                config, namespace=request.namespace, sample_id=sample_id, sample_dir=sample_dir
            )
            update_manifest_reachability(sample_dir, record["reachability"])
        return record
    try:
        command = build_command(request)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        if args.dry_run:
            return {
                "sample": sample_id,
                "status": "failed",
                "returncode": None,
                "seconds": round(time.monotonic() - started, 1),
                "results_dir": str(sample_dir),
                "error": error,
            }
        write_failure_artifact(
            sample_dir,
            sample_id=sample_id,
            harness=request.harness,
            model=request.model,
            framework="poc_generation",
            status="error",
            stop_reason="build_command_failed",
            error=error,
            seconds=round(time.monotonic() - started, 1),
            extra={
                "namespace": request.namespace,
                "results_dir": str(sample_dir),
            },
            overwrite_manifest=True,
        )
        return {
            "sample": sample_id,
            "status": "failed",
            "returncode": None,
            "seconds": round(time.monotonic() - started, 1),
            "results_dir": str(sample_dir),
            "error": error,
        }
    if args.dry_run:
        return {"sample": sample_id, "status": "planned", "command": command.redacted()}

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_dir = LOG_ROOT / request.namespace
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{sample_id}.log"
    proc: subprocess.CompletedProcess[str] | None = None
    run_error: str | None = None
    with log_path.open("w", encoding="utf-8") as log:
        log.write("COMMAND " + json.dumps(command.redacted(), ensure_ascii=False) + "\n")
        log.flush()
        try:
            proc = subprocess.run(
                command.command,
                cwd=command.cwd,
                env=dict(command.env),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            run_error = f"{type(exc).__name__}: {exc}"
            log.write(f"\n[batch-runner] adapter launch failed: {run_error}\n")
    record = {
        "sample": sample_id,
        "status": "complete" if proc is not None and proc.returncode == 0 else "failed",
        "returncode": proc.returncode if proc is not None else None,
        "seconds": round(time.monotonic() - started, 1),
        "results_dir": str(sample_dir),
        "log": str(log_path),
    }
    if run_error:
        record["error"] = run_error
    has_sample_artifact = (
        (sample_dir / "manifest.json").is_file()
        and (sample_dir / "checkpoint").is_dir()
    )
    if proc is not None and proc.returncode == 0 and has_sample_artifact:
        record["reachability"] = maybe_run_reachability(
            config, namespace=request.namespace, sample_id=sample_id, sample_dir=sample_dir
        )
        update_manifest_reachability(sample_dir, record["reachability"])
    else:
        if proc is not None and proc.returncode == 0:
            record["status"] = "failed"
            record["error"] = "adapter returned 0 without manifest/checkpoint"
        if not has_sample_artifact:
            checkpoint_files = {}
            partial_manifest = sample_dir / "manifest.json"
            if partial_manifest.is_file():
                checkpoint_files["partial_manifest.json"] = partial_manifest
            write_failure_artifact(
                sample_dir,
                sample_id=sample_id,
                harness=request.harness,
                model=request.model,
                framework="poc_generation",
                status="error",
                stop_reason=(
                    "adapter_launch_failed"
                    if proc is None
                    else (
                        "adapter_returned_zero_without_sample_artifact"
                        if proc.returncode == 0
                        else "adapter_returned_nonzero_without_manifest"
                    )
                ),
                error=(
                    run_error
                    or (
                        "adapter returned 0 without manifest/checkpoint"
                        if proc is not None and proc.returncode == 0
                        else f"adapter exited with returncode {proc.returncode}"
                    )
                ),
                returncode=proc.returncode if proc is not None else None,
                seconds=record["seconds"],
                command=command.redacted(),
                log_path=log_path,
                checkpoint_files=checkpoint_files,
                extra={
                    "namespace": request.namespace,
                    "results_dir": str(sample_dir),
                },
                overwrite_manifest=True,
            )
    return record


def record_unhandled_sample_failure(
    args: argparse.Namespace,
    config: dict[str, Any],
    sample_id: str,
    exc: BaseException,
) -> dict[str, Any]:
    started = time.monotonic()
    harness = str(_config_value(args, config, "harness", "unknown") or "unknown")
    model = str(_config_value(args, config, "model", "") or "")
    namespace = str(
        _config_value(args, config, "namespace")
        or config.get("results_namespace")
        or model
        or "unknown"
    )
    sample_dir = (REPO_ROOT / "poc_generation" / "poc_results" / namespace) / sample_id
    error = f"{type(exc).__name__}: {exc}"
    write_failure_artifact(
        sample_dir,
        sample_id=sample_id,
        harness=harness,
        model=model,
        framework="poc_generation",
        status="error",
        stop_reason="batch_runner_exception",
        error=error,
        seconds=round(time.monotonic() - started, 1),
        extra={
            "namespace": namespace,
            "results_dir": str(sample_dir),
        },
        overwrite_manifest=True,
    )
    return {
        "sample": sample_id,
        "status": "failed",
        "returncode": None,
        "seconds": round(time.monotonic() - started, 1),
        "results_dir": str(sample_dir),
        "error": error,
    }


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
    parser.add_argument("--model-route")
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
            sample = futures[future]
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001
                record = record_unhandled_sample_failure(args, config, sample, exc)
            counts[record["status"]] = counts.get(record["status"], 0) + 1
            print(json.dumps(record, ensure_ascii=False), flush=True)
    print(json.dumps({"final_counts": counts}, ensure_ascii=False), flush=True)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
