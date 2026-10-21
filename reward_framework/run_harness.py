#!/usr/bin/env python3
"""Run skill-enabled PoC evaluations through one reward-framework adapter."""

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

# Keep the ordinary reward entry point on the inexpensive canonical arm.  The
# 32768-token arm remains available by passing its config explicitly.
DEFAULT_REWARD_CONFIG = (
    REPO_ROOT / "reward_framework" / "configs" / "dsh_deepseek_4096_max.json"
)

from harness_runtime.analysis_finalizer import finalize_checkpoint_analysis
from harness_runtime.arvo_submission import verify_reachability_debugger_image
from harness_runtime.failure_artifact import write_failure_artifact
from harness_runtime.provider_protocol import resolve_analysis_transport
from poc_generation.extract_context_visit import write_context_visit
from reward_framework.adapters import HARNESSES, RewardRequest, build_command
from reward_framework.adapters.base import DEFAULT_RUNS_ROOT, DEFAULT_SKILL_PACKET
from runtime_credentials import credential_value, load_runtime_credentials


VALID_CODING_TERMINAL_STATUSES = {"success", "agent_finished", "iteration_cap"}


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


def _config_value(
    args: argparse.Namespace,
    config: dict[str, Any],
    name: str,
    default: Any = None,
) -> Any:
    value = getattr(args, name, None)
    if value not in (None, "", 0):
        return value
    return config.get(name, default)


def _now_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _no_skill_requested(args: argparse.Namespace, config: dict[str, Any]) -> bool:
    return bool(getattr(args, "no_skill", False) or config.get("no_skill"))


def _skill_packet_from_config(
    args: argparse.Namespace,
    config: dict[str, Any],
) -> Path | None:
    if _no_skill_requested(args, config):
        return None
    explicit = getattr(args, "skill_packet", None)
    if explicit not in (None, ""):
        return Path(str(explicit)).expanduser().resolve()
    variant = getattr(args, "skill_variant", None) or config.get("skill_variant")
    variants = config.get("skill_packets")
    if variant and isinstance(variants, dict):
        selected = variants.get(str(variant))
        if selected in (None, ""):
            raise ValueError(
                f"unknown skill_variant {variant!r}; expected one of "
                f"{sorted(str(key) for key in variants)}"
            )
        return Path(str(selected)).expanduser().resolve()
    return Path(
        str(_config_value(args, config, "skill_packet", str(DEFAULT_SKILL_PACKET)))
    ).expanduser().resolve()


def load_samples(args: argparse.Namespace, config: dict[str, Any]) -> list[str]:
    # Explicit command-line rosters are selectors, not additions to the
    # canonical split.  This makes ``--sample`` genuinely useful for a smoke
    # run while an invocation without a selector still uses the frozen 100/100
    # partition from the config.
    cli_samples = [
        str(item).strip()
        for item in getattr(args, "sample", [])
        if str(item).strip()
    ]
    cli_samples_file = str(getattr(args, "samples_file", "") or "").strip()
    cli_selector = str(getattr(args, "sample_selector", "") or "").strip()
    if cli_samples:
        samples = cli_samples
    elif cli_samples_file:
        samples = _read_samples_file(Path(cli_samples_file).expanduser().resolve())
    elif cli_selector:
        samples = _selected_valid_gt_samples(cli_selector)
    else:
        samples = []
        split_manifest = str(config.get("split_manifest") or "").strip()
        if split_manifest:
            partition = str(config.get("split_partition") or "test").strip()
            payload = _load_json(Path(split_manifest).expanduser().resolve())
            split_samples = payload.get(partition)
            if not isinstance(split_samples, list) or not all(
                isinstance(sample, str) and sample.strip() for sample in split_samples
            ):
                raise ValueError(
                    f"split manifest {split_manifest!r} has no non-empty list at {partition!r}"
                )
            samples.extend(sample.strip() for sample in split_samples)
        selector = _config_value(args, config, "sample_selector", "")
        if selector:
            samples.extend(_selected_valid_gt_samples(str(selector)))
        samples.extend(
            str(item).strip() for item in config.get("samples", []) if str(item).strip()
        )
        samples_file = str(config.get("samples_file") or "").strip()
        if samples_file:
            samples.extend(_read_samples_file(Path(samples_file).expanduser().resolve()))
    start = int(getattr(args, "start_index", 0) or 0)
    limit = int(getattr(args, "limit", 0) or 0)
    selected = list(dict.fromkeys(samples))[start:]
    if limit > 0:
        selected = selected[:limit]
    if not selected:
        raise ValueError("no samples selected")
    return selected


def _read_samples_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if text.startswith("["):
        values = json.loads(text)
        if not isinstance(values, list):
            raise ValueError(f"samples file {path!s} must contain a JSON list")
        return [str(item).strip() for item in values if str(item).strip()]
    return [line.strip() for line in text.splitlines() if line.strip()]


def result_is_complete(sample_dir: Path) -> bool:
    manifest = sample_dir / "manifest.json"
    checkpoint = sample_dir / "checkpoint"
    if not manifest.is_file() or not checkpoint.is_dir():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("status") not in VALID_CODING_TERMINAL_STATUSES:
        return False
    if not payload.get("workspace_adapter"):
        return False
    # The coding episode is complete even when its optional artifact is absent
    # or invalid. The separate finalizer repairs it from checkpoint evidence;
    # this branch must never authorize a new coding run.
    return True


def _sample_has_submitted_candidate(sample_dir: Path) -> bool:
    manifest = sample_dir / "manifest.json"
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    deduplicated = payload.get("deduplicated_pocs")
    if isinstance(deduplicated, list) and deduplicated:
        return True
    attempts = payload.get("submission_attempts")
    if isinstance(attempts, list) and attempts:
        return True
    return int(payload.get("num_submission_attempts") or 0) > 0


def _agent_terminal_status(sample_dir: Path) -> str:
    manifest = sample_dir / "manifest.json"
    if not manifest.is_file():
        return "missing_manifest"
    try:
        return str(_load_json(manifest).get("status") or "missing_status")
    except (OSError, json.JSONDecodeError):
        return "invalid_manifest"


def maybe_run_reachability(
    config: dict[str, Any],
    *,
    run_id: str,
    sample_id: str,
    sample_dir: Path,
) -> dict[str, Any]:
    if not config.get("run_reachability_after_generation", True):
        return {"status": "disabled"}
    try:
        from evaluator.reachability.eval_batch import evaluate_model_sample

        result = evaluate_model_sample(
            model=run_id,
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


def preflight_batch_environment(config: dict[str, Any]) -> dict[str, Any]:
    """Fail before coding when the shared evaluator runtime is unavailable."""
    if not config.get("run_reachability_after_generation", True):
        return {"reachability": {"status": "disabled"}}
    image = str(config.get("reachability_debugger_image") or "gt-memory-env:latest")
    return {"reachability": verify_reachability_debugger_image(image)}


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


def existing_reachability(sample_dir: Path) -> dict[str, Any] | None:
    report_path = sample_dir / "reachability_eval.json"
    if not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "present", "path": "reachability_eval.json"}
    if isinstance(report, dict) and isinstance(report.get("summary"), dict):
        return {"status": "complete", "summary": report["summary"]}
    return {"status": "present", "path": "reachability_eval.json"}


def maybe_write_context_visit(sample_dir: Path) -> dict[str, Any]:
    """Materialize context_visit.json from the saved agent checkpoint."""
    if not (sample_dir / "checkpoint").is_dir():
        return {"status": "skipped", "reason": "checkpoint missing"}
    try:
        written = write_context_visit(
            sample_dir,
            overwrite=True,
            update_manifest_flag=True,
        )
        report = _load_json(sample_dir / "context_visit.json")
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    collection = report.get("collection") or {}
    return {
        "status": "complete",
        "written": written,
        "recoverable": bool(collection.get("recoverable")),
        "context_count": int(collection.get("context_count") or 0),
    }


def build_request(
    args: argparse.Namespace,
    config: dict[str, Any],
    *,
    run_id: str,
    run_dir: Path,
    sample_id: str,
) -> RewardRequest:
    harness = str(_config_value(args, config, "harness", "openhands"))
    load_runtime_credentials()
    model = str(_config_value(args, config, "model", "")).strip()
    if not model:
        raise ValueError("config.model is required")
    base_url = str(_config_value(args, config, "base_url", "")).strip()
    if not base_url:
        base_url_env = str(config.get("base_url_env") or "").strip()
        if base_url_env:
            base_url = credential_value(base_url_env)
    skill_packet = _skill_packet_from_config(args, config)
    results_dir = run_dir / "results"
    max_effective = _config_value(args, config, "max_effective_submits")
    extra_args = [str(item) for item in config.get("extra_args", [])]
    dsh_provider = str(_config_value(args, config, "dsh_provider", "")).strip()
    if dsh_provider and harness == "deepseek_harness":
        extra_args.extend(["--dsh-provider", dsh_provider])
    return RewardRequest(
        harness=harness,
        sample_id=sample_id,
        model=model,
        run_id=run_id,
        results_dir=results_dir,
        skill_packet=skill_packet,
        base_url=base_url,
        api_key_env=str(_config_value(args, config, "api_key_env", "")),
        api_version=str(_config_value(args, config, "api_version", "")),
        max_iter=int(_config_value(args, config, "max_iter", 100)),
        # This count belongs exclusively to the post-run checkpoint
        # finalizer.  Adapters receive a fixed one coding trajectory.
        max_attempts=int(
            config.get("analysis_attempts")
            or config.get("max_attempts")
            or args.max_attempts
        ),
        timeout=int(_config_value(args, config, "timeout", 10800)),
        server=str(_config_value(args, config, "server", "http://host.docker.internal:8666")),
        difficulty=str(_config_value(args, config, "difficulty", "level1")),
        openhands_repo=(
            Path(config["openhands_repo"]).expanduser()
            if config.get("openhands_repo")
            else None
        ),
        max_effective_submits=(
            int(max_effective) if max_effective not in (None, "", 0) else None
        ),
        reasoning_effort=str(_config_value(args, config, "reasoning_effort", "medium")),
        max_output_tokens=int(_config_value(args, config, "max_output_tokens", 4096)),
        extra_args=tuple(extra_args),
    )


def maybe_finalize_analysis(
    config: dict[str, Any], request: RewardRequest, log_path: Path
) -> dict[str, Any]:
    """Finalize the optional artifact from a completed saved checkpoint only."""
    transport = resolve_analysis_transport(
        str(config.get("analysis_base_url") or request.base_url),
        str(config.get("analysis_transport") or ""),
    )
    return finalize_checkpoint_analysis(
        sample_id=request.sample_id,
        results_dir=request.results_dir,
        model=str(config.get("analysis_model") or request.model),
        base_url=str(config.get("analysis_base_url") or request.base_url),
        api_key_env=str(config.get("analysis_api_key_env") or request.api_key_env),
        api_version=str(config.get("analysis_api_version") or request.api_version),
        reasoning_effort=str(
            config.get("analysis_reasoning_effort") or request.reasoning_effort or ""
        ),
        max_output_tokens=int(
            config.get("analysis_max_output_tokens") or request.max_output_tokens or 0
        ),
        max_attempts=request.max_attempts,
        transport=transport,
        max_chars=int(config.get("analysis_max_chars") or 220000),
        log_path=log_path,
    )


def run_one(
    args: argparse.Namespace,
    config: dict[str, Any],
    *,
    run_id: str,
    run_dir: Path,
    sample_id: str,
) -> dict[str, Any]:
    started = time.monotonic()
    request = build_request(args, config, run_id=run_id, run_dir=run_dir, sample_id=sample_id)
    sample_dir = request.results_dir / sample_id
    if sample_dir.exists() and not args.overwrite and result_is_complete(sample_dir):
        record: dict[str, Any] = {
            "sample": sample_id,
            "status": "skipped",
            "results_dir": str(sample_dir),
        }
        # The coding trajectory is already terminal.  A missing artifact is
        # finalized in place rather than replaying the coding sample.
        record["analysis_finalization"] = maybe_finalize_analysis(
            config, request, run_dir / "logs" / f"{sample_id}.log"
        )
        reachability = existing_reachability(sample_dir)
        if reachability is None:
            reachability = maybe_run_reachability(
                config, run_id=run_id, sample_id=sample_id, sample_dir=sample_dir
            )
        record["reachability"] = reachability
        update_manifest_reachability(sample_dir, reachability)
        return record
    # Preserve an incomplete agent checkpoint when the experiment explicitly
    # asks for it.  Missing analysis.json is a measured outcome, not an
    # implicit authorization to replace the trajectory with a fresh attempt.
    if (
        sample_dir.exists()
        and not args.overwrite
        and config.get("preserve_existing_failures", False)
        and (sample_dir / "manifest.json").is_file()
    ):
        try:
            previous = _load_json(sample_dir / "manifest.json")
        except (OSError, json.JSONDecodeError):
            previous = {}
        return {
            "sample": sample_id,
            "status": "preserved",
            "previous_status": previous.get("status") or "unknown",
            "results_dir": str(sample_dir),
        }
    if request.skill_packet is not None and not request.skill_packet.is_dir():
        error = f"FileNotFoundError: skill packet not found: {request.skill_packet}"
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
            framework="reward_framework",
            status="error",
            stop_reason="skill_packet_missing",
            error=error,
            seconds=round(time.monotonic() - started, 1),
            extra={
                "run_id": run_id,
                "results_dir": str(sample_dir),
                "skill_packet": str(request.skill_packet) if request.skill_packet else None,
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
            framework="reward_framework",
            status="error",
            stop_reason="build_command_failed",
            error=error,
            seconds=round(time.monotonic() - started, 1),
            extra={
                "run_id": run_id,
                "results_dir": str(sample_dir),
                "skill_packet": str(request.skill_packet) if request.skill_packet else None,
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

    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    request.results_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{sample_id}.log"
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
    agent_terminal_status = _agent_terminal_status(sample_dir) if has_sample_artifact else ""
    if has_sample_artifact:
        record["agent_terminal_status"] = agent_terminal_status
    if (sample_dir / "checkpoint").is_dir():
        record["context_visit"] = maybe_write_context_visit(sample_dir)
    if has_sample_artifact:
        if agent_terminal_status not in VALID_CODING_TERMINAL_STATUSES:
            record["status"] = "failed"
            record["error"] = (
                "invalid coding terminal status: "
                f"{agent_terminal_status}; only agent_finished or iteration_cap are valid"
            )
        else:
            if record["status"] == "failed":
                record["adapter_returncode"] = record["returncode"]
                record["status"] = "complete"
            record["analysis_finalization"] = maybe_finalize_analysis(
                config, request, log_path
            )
            reachability = existing_reachability(sample_dir)
            if reachability is None:
                reachability = maybe_run_reachability(
                    config, run_id=run_id, sample_id=sample_id, sample_dir=sample_dir
                )
            record["reachability"] = reachability
            update_manifest_reachability(sample_dir, reachability)
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
                framework="reward_framework",
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
                    "run_id": run_id,
                    "results_dir": str(sample_dir),
                    "skill_packet": str(request.skill_packet) if request.skill_packet else None,
                },
                overwrite_manifest=True,
            )
    return record


def record_unhandled_sample_failure(
    args: argparse.Namespace,
    config: dict[str, Any],
    *,
    run_id: str,
    run_dir: Path,
    sample_id: str,
    exc: BaseException,
) -> dict[str, Any]:
    started = time.monotonic()
    harness = str(_config_value(args, config, "harness", "unknown") or "unknown")
    model = str(_config_value(args, config, "model", "") or "")
    sample_dir = run_dir / "results" / sample_id
    skill_packet = _skill_packet_from_config(args, config)
    error = f"{type(exc).__name__}: {exc}"
    write_failure_artifact(
        sample_dir,
        sample_id=sample_id,
        harness=harness,
        model=model,
        framework="reward_framework",
        status="error",
        stop_reason="batch_runner_exception",
        error=error,
        seconds=round(time.monotonic() - started, 1),
        extra={
            "run_id": run_id,
            "results_dir": str(sample_dir),
            "skill_packet": str(skill_packet) if skill_packet else None,
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


def write_run_manifest(
    run_dir: Path,
    *,
    run_id: str,
    config_path: Path | None,
    config: dict[str, Any],
    samples: list[str],
    args: argparse.Namespace,
    batch_preflight: dict[str, Any],
) -> None:
    harness = str(_config_value(args, config, "harness", "openhands"))
    skill_packet = _skill_packet_from_config(args, config)
    skill_variant = getattr(args, "skill_variant", None) or config.get("skill_variant")
    analysis_base_url = str(
        config.get("analysis_base_url") or _config_value(args, config, "base_url", "")
    )
    analysis_transport = resolve_analysis_transport(
        analysis_base_url,
        str(config.get("analysis_transport") or ""),
    )
    manifest = {
        "run_id": run_id,
        "framework": "reward_framework",
        "harness": harness.strip().lower().replace("-", "_"),
        "entrypoint": "reward_framework.run_harness",
        "results_dir": str(run_dir / "results"),
        "logs_dir": str(run_dir / "logs"),
        "config_path": str(config_path) if config_path else None,
        "model": _config_value(args, config, "model"),
        "base_url_configured": bool(_config_value(args, config, "base_url", "")),
        "api_key_env": _config_value(args, config, "api_key_env", ""),
        "skill_packet": str(skill_packet) if skill_packet else None,
        "skill_variant": str(skill_variant) if skill_variant else None,
        "no_skill": skill_packet is None,
        "max_effective_submits": _config_value(args, config, "max_effective_submits"),
        "coding_attempts_per_sample": 1,
        "analysis_finalizer_max_attempts": int(
            config.get("analysis_attempts")
            or config.get("max_attempts")
            or args.max_attempts
        ),
        "analysis_transport": analysis_transport,
        "analysis_base_url_configured": bool(analysis_base_url),
        "analysis_protocol": "checkpoint_finalizer_v1",
        "batch_preflight": batch_preflight,
        "samples": samples,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # `config` is the input file, while CLI flags intentionally override it.
    # Persist the resolved values rather than misleadingly labelling the raw
    # input as the effective experiment configuration.
    effective_config = dict(config)
    effective_config.update({
        "run_id": run_id,
        "source_config_path": str(config_path) if config_path else None,
        "harness": harness,
        "model": _config_value(args, config, "model"),
        "base_url": _config_value(args, config, "base_url", ""),
        "api_key_env": _config_value(args, config, "api_key_env", ""),
        "api_version": _config_value(args, config, "api_version", ""),
        "analysis_transport": analysis_transport,
        "analysis_base_url": analysis_base_url,
        "analysis_api_key_env": str(
            config.get("analysis_api_key_env")
            or _config_value(args, config, "api_key_env", "")
        ),
        "dsh_provider": _config_value(args, config, "dsh_provider", ""),
        "skill_packet": str(skill_packet) if skill_packet else None,
        "skill_variant": str(skill_variant) if skill_variant else None,
        "no_skill": skill_packet is None,
        "max_iter": int(_config_value(args, config, "max_iter", 100)),
        "max_attempts": int(
            config.get("analysis_attempts")
            or config.get("max_attempts")
            or args.max_attempts
        ),
        "max_effective_submits": _config_value(args, config, "max_effective_submits"),
        "reasoning_effort": _config_value(args, config, "reasoning_effort", "medium"),
        "max_output_tokens": int(_config_value(args, config, "max_output_tokens", 4096)),
        "timeout": int(_config_value(args, config, "timeout", 10800)),
        "parallel": int(args.parallel),
        "samples": samples,
    })
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "run_config_effective.json").write_text(
        json.dumps(effective_config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_REWARD_CONFIG,
        help=(
            "reward config (default: dsh_deepseek_4096_max.json; use "
            "dsh_deepseek_32768_max.json explicitly for the high-budget arm)"
        ),
    )
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--samples-file")
    parser.add_argument("--sample-selector")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--skill-packet")
    parser.add_argument(
        "--skill-variant",
        choices=("initial", "evolving"),
        help="select a named skill packet from config.skill_packets; --skill-packet wins",
    )
    parser.add_argument("--no-skill", action="store_true")
    parser.add_argument("--harness", choices=HARNESSES)
    parser.add_argument("--model")
    parser.add_argument("--model-route")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env")
    parser.add_argument("--api-version")
    parser.add_argument(
        "--dsh-provider",
        choices=("gt-openai-compatible", "deepseek-official"),
    )
    parser.add_argument("--max-iter", type=int)
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=1,
        help="Maximum post-run checkpoint-finalizer LLM calls; coding runs once.",
    )
    parser.add_argument("--max-effective-submits", type=int)
    parser.add_argument("--reasoning-effort")
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        choices=(4096, 32768),
        default=None,
        help="coding-agent output-token cap (default: 4096 via the default config)",
    )
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--server")
    parser.add_argument("--difficulty")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = args.config.expanduser().resolve() if args.config else None
    config = _load_json(config_path) if config_path else {}
    samples = load_samples(args, config)
    run_id = args.run_id or str(config.get("run_id") or _now_id())
    run_dir = args.runs_root.expanduser().resolve() / run_id
    batch_preflight = preflight_batch_environment(config)
    print(json.dumps({"batch_preflight": batch_preflight}, ensure_ascii=False), flush=True)
    write_run_manifest(
        run_dir,
        run_id=run_id,
        config_path=config_path,
        config=config,
        samples=samples,
        args=args,
        batch_preflight=batch_preflight,
    )

    counts: dict[str, int] = {}
    status_path = run_dir / "status.jsonl"
    with ThreadPoolExecutor(max_workers=max(1, int(args.parallel))) as executor:
        futures = {
            executor.submit(
                run_one,
                args,
                config,
                run_id=run_id,
                run_dir=run_dir,
                sample_id=sample,
            ): sample
            for sample in samples
        }
        with status_path.open("a", encoding="utf-8") as status_file:
            for future in as_completed(futures):
                sample = futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001
                    record = record_unhandled_sample_failure(
                        args,
                        config,
                        run_id=run_id,
                        run_dir=run_dir,
                        sample_id=sample,
                        exc=exc,
                    )
                counts[record["status"]] = counts.get(record["status"], 0) + 1
                line = json.dumps(record, ensure_ascii=False)
                print(line, flush=True)
                status_file.write(line + "\n")
                status_file.flush()
    summary = {
        "run_id": run_id,
        "counts": counts,
        "results_dir": str(run_dir / "results"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"final_counts": counts}, ensure_ascii=False), flush=True)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
