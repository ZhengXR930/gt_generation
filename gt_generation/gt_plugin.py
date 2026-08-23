#!/usr/bin/env python3
"""Config-driven launcher for fine-grained GT generation.

A single JSON config (see gt_config.example.json) drives everything: which agent
CLI to use (claude / codex / coco), the one model to run every stage with, how
many samples to generate in parallel, and the sample id list. Start it once and
it generates GT for the whole list unattended:

    python3 gt_generation/gt_plugin.py --config gt_generation/gt_config.json

It is a thin orchestrator: per sample it invokes runner.py (the proven stage
engine) with the selected adapter wired in via GT_AGENT_COMMAND, and lets
gt_toolkit/prepare.py route Docker (ARVO samples -> original n132/arvo:<id>
images; everything else -> the shared gt-memory-env image). The launcher never
touches Docker itself; it only makes the routing decision visible up front.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parent          # gt_generation/
REPO_ROOT = CODE_ROOT.parent                          # repo root
RUNNER = CODE_ROOT / "runner.py"

# cli name -> adapter script honoring runner.py's GT_AGENT_COMMAND contract
# (--role-file / --sample / --result-dir).
ADAPTERS = {
    "claude": CODE_ROOT / "adapters" / "claude_code" / "gt_agent_claude.sh",
    "codex": CODE_ROOT / "adapters" / "codex" / "gt_agent_codex.sh",
    "coco": CODE_ROOT / "adapters" / "coco" / "gt_agent_coco.sh",
}
MAX_PARALLEL = 6  # local Docker budget ceiling
CODEX_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
CODEX_WIRE_APIS = {"responses"}


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict[str, Any]:
    """Read and validate the launcher config, applying defaults."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"config must be a JSON object: {path}")

    cli = str(raw.get("cli") or "").strip().lower()
    if cli not in ADAPTERS:
        raise SystemExit(f"config.cli must be one of {sorted(ADAPTERS)}; got {cli!r}")
    adapter = ADAPTERS[cli]
    if not adapter.is_file():
        raise SystemExit(f"adapter for cli {cli!r} not found: {adapter}")

    model = str(raw.get("model") or "").strip()
    if not model:
        raise SystemExit("config.model is required (one model id for every stage)")

    reasoning_effort = str(raw.get("reasoning_effort") or "high").strip().lower()
    if cli == "codex" and reasoning_effort not in CODEX_REASONING_EFFORTS:
        raise SystemExit(
            "config.reasoning_effort must be one of "
            f"{sorted(CODEX_REASONING_EFFORTS)} for codex; got {reasoning_effort!r}"
        )
    strict_config = bool(raw.get("strict_config", True))
    codex_provider = load_codex_provider(raw.get("codex_provider"), cli)

    repo_docker_image = str(raw.get("repo_docker_image") or "gt-memory-env:latest").strip()
    repo_docker_context = Path(
        str(raw.get("repo_docker_context") or "docker/gt-memory-env")
    )
    if not repo_docker_context.is_absolute():
        repo_docker_context = REPO_ROOT / repo_docker_context
    if not repo_docker_context.is_dir():
        raise SystemExit(f"config.repo_docker_context is not a directory: {repo_docker_context}")

    parallel = int(raw.get("parallel_dockers") or 1)
    if not 1 <= parallel <= MAX_PARALLEL:
        raise SystemExit(f"config.parallel_dockers must be between 1 and {MAX_PARALLEL}")

    samples = raw.get("samples") or []
    if not isinstance(samples, list) or not samples:
        raise SystemExit("config.samples must be a non-empty list of sample ids")
    # de-duplicate while preserving order
    samples = list(dict.fromkeys(str(s) for s in samples))

    selection = str(raw.get("selection") or "dataset/selected_1000.json")
    selection_path = Path(selection)
    if not selection_path.is_absolute():
        selection_path = REPO_ROOT / selection_path

    start_at = str(raw.get("start_at") or "").strip()
    stop_after = str(raw.get("stop_after") or "").strip()
    if start_at == "04_assertion_validator":
        start_at = "04_assertion_plan"
    if stop_after == "04_assertion_validator":
        stop_after = "04_assertion_plan"
    repair_stages = {
        "02_fine_trace",
        "03_trace_review",
        "04_assertion_plan",
        "04_instrument_vulnerable",
        "04_instrument_fixed",
        "04_assertion_execute",
        "04_reachability",
    }
    if start_at in repair_stages:
        if stop_after and stop_after != "05_validate":
            raise SystemExit(
                "partial GT repairs must run through 05_validate; "
                "use runner.py directly for non-publishing stage diagnostics"
            )
        stop_after = "05_validate"

    run_mode = "stage01_screening" if (
        not start_at and stop_after == "01_reproducer"
    ) else "full_gt_generation"
    stage01_migration = bool(raw.get("stage01_migration", False))
    if stage01_migration and run_mode != "stage01_screening":
        raise SystemExit(
            "stage01_migration requires stop_after='01_reproducer' and no start_at"
        )

    return {
        "cli": cli,
        "adapter": adapter,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "strict_config": strict_config,
        "codex_provider": codex_provider,
        "repo_docker_image": repo_docker_image,
        "repo_docker_context": repo_docker_context,
        "parallel_dockers": parallel,
        "samples": samples,
        "selection_path": selection_path,
        # Re-run only the stages that have not passed. A sample that died late
        # keeps its accepted clone, reproduction and fine trace.
        "resume": bool(raw.get("resume", False)),
        "reuse_repair_staging": bool(raw.get("reuse_repair_staging", False)),
        "start_at": start_at,
        "stop_after": stop_after,
        "run_mode": run_mode,
        "stage01_migration": stage01_migration,
    }


def load_codex_provider(raw: Any, cli: str) -> dict[str, Any]:
    if raw in (None, {}, ""):
        return {}
    if cli != "codex":
        raise SystemExit("config.codex_provider is only valid when config.cli is 'codex'")
    if not isinstance(raw, dict):
        raise SystemExit("config.codex_provider must be a JSON object")

    provider_id = str(raw.get("id") or "").strip()
    base_url = str(raw.get("base_url") or "").strip()
    if not provider_id:
        raise SystemExit("config.codex_provider.id is required")
    if provider_id in {"openai", "azure", "oss", "chatgpt"}:
        raise SystemExit("config.codex_provider.id must not be a reserved built-in provider id")
    if not base_url:
        raise SystemExit("config.codex_provider.base_url is required")

    wire_api = str(raw.get("wire_api") or "responses").strip().lower()
    if wire_api not in CODEX_WIRE_APIS:
        raise SystemExit(
            "config.codex_provider.wire_api must be one of "
            f"{sorted(CODEX_WIRE_APIS)} for this Codex CLI; got {wire_api!r}"
        )

    provider = {
        "id": provider_id,
        "name": str(raw.get("name") or provider_id).strip(),
        "base_url": base_url,
        "wire_api": wire_api,
    }
    env_key = str(raw.get("env_key") or "").strip()
    if env_key:
        provider["env_key"] = env_key
    bridge = raw.get("bridge")
    if bridge not in (None, {}, ""):
        if not isinstance(bridge, dict):
            raise SystemExit("config.codex_provider.bridge must be a JSON object")
        if bridge.get("enabled") is not True:
            raise SystemExit("config.codex_provider.bridge.enabled must be true when bridge is set")
        target_url = str(bridge.get("target_url") or "").strip()
        if not target_url:
            raise SystemExit("config.codex_provider.bridge.target_url is required")
        provider["bridge"] = {
            "enabled": True,
            "target_url": target_url,
            "max_tokens": str(int(bridge.get("max_tokens") or 16384)),
            "timeout_seconds": str(int(bridge.get("timeout_seconds") or 600)),
        }
    return provider


def load_selection(path: Path) -> dict[str, dict[str, Any]]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise SystemExit(f"selection must be a JSON list: {path}")
    return {str(v["sample_id"]): v for v in values if isinstance(v, dict) and v.get("sample_id")}


# --------------------------------------------------------------------------- #
# Docker routing (decision only -- prepare.py does the actual work)            #
# --------------------------------------------------------------------------- #
def docker_track(sample: dict[str, Any]) -> str:
    """'arvo' (original n132/arvo:<id> images) or 'repo' (shared gt-memory-env),
    using the same signal prepare.py routes on, so the printed plan matches what
    stage 00 will actually do."""
    try:
        from gt_toolkit.prepare import _is_arvo  # type: ignore
        return "arvo" if _is_arvo(sample) else "repo"
    except Exception:
        is_arvo = bool(
            sample.get("arvo_image_vul")
            or str(sample.get("source_dataset", "")).upper().startswith("ARVO")
        )
        return "arvo" if is_arvo else "repo"


def cleanup_arvo(sample_id: str) -> None:
    """Drop the per-sample ARVO container/images after a run so parallel workers
    do not exhaust local disk. No-op for repo-track samples."""
    arvo_id = sample_id[5:] if sample_id.startswith("arvo_") else sample_id
    subprocess.run(
        ["docker", "rm", "-f", f"gt-arvo_{arvo_id}-workspace"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for image in (f"n132/arvo:{arvo_id}-vul", f"n132/arvo:{arvo_id}-fix"):
        subprocess.run(
            ["docker", "image", "rm", image],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def current_stage(result_dir: Path) -> str:
    try:
        state = json.loads((result_dir / "gt_generation_state.json").read_text(encoding="utf-8"))
    except Exception:
        return "starting"
    return str(state.get("current_stage") or "starting")


def completed_samples_to_skip(
    samples: list[str], cfg: dict[str, Any]
) -> list[str]:
    """Ordinary batches skip complete GT; explicit partial reruns repair it."""
    if cfg.get("start_at"):
        return []
    if cfg.get("run_mode") == "stage01_screening":
        # Audit-complete legacy packages still need the new deterministic
        # portability proof.  A Stage-01 migration skips only packages that
        # already passed that proof, not every package with complete semantics.
        try:
            from gt_toolkit.portability import portability_gate_passes
        except ImportError:
            from gt_generation.gt_toolkit.portability import portability_gate_passes

        return [
            sample_id
            for sample_id in samples
            if portability_gate_passes(REPO_ROOT / "gt_results" / sample_id)
        ]
    import gt_status

    return [
        sample_id
        for sample_id in samples
        if gt_status.classify(sample_id)[0] == "complete"
    ]


def _load_json_or_none(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _setup_command_masks_failures(command: str) -> bool:
    if re.search(r"\|\|\s*(?:true|:)(?:\s|$|[;&|)'\"`])", command):
        return True
    if re.search(r"(?:^|[\s;'\"`])set\s+\+e(?:$|[\s;'\"`])", command):
        return True
    return False


def _repo_fixed_oracle_required(result_dir: Path, sample: dict[str, Any]) -> bool:
    prepare = _load_json_or_none(result_dir / "prepare_report.json")
    info = _load_json_or_none(result_dir / "sample_info.json")
    track = str((prepare or {}).get("track") or "")
    if not track.startswith("repo/"):
        return False
    fix_commit = str(
        (info or {}).get("fix_commit")
        or (info or {}).get("fixed_commit")
        or sample.get("fix_commit")
        or sample.get("fixed_commit")
        or ""
    ).strip()
    return bool(fix_commit)


def evaluate_stage01_screening(
    result_dir: Path, sample: dict[str, Any], runner_returncode: int
) -> dict[str, Any]:
    """Classify a Stage-01-only run without requiring a completed GT package."""
    report_path = result_dir / "reproduction_report.json"
    report = _load_json_or_none(report_path)
    fixed_required = _repo_fixed_oracle_required(result_dir, sample)
    status = "incomplete_stage01"
    reason = "missing_or_invalid_reproduction_report"
    accepted = False
    portability_path = result_dir / "portability_report.json"
    portability = _load_json_or_none(portability_path)

    if isinstance(report, dict):
        reproduced = report.get("vulnerable_reproduced") is True
        matches_issue = report.get("matches_issue") is True
        fixed = report.get("fixed_oracle")
        fixed_checked = report.get("fixed_oracle_checked")
        fixed_acceptable = report.get("fixed_oracle_acceptable")
        if isinstance(fixed, dict):
            fixed_checked = fixed.get("checked", fixed_checked)
            fixed_acceptable = fixed.get("acceptable", fixed_acceptable)
        masked_setup = _setup_command_masks_failures(str(report.get("setup_command") or ""))

        if not reproduced:
            status = "rejected_by_stage01"
            reason = "vulnerable_reproduction_not_established"
        elif not matches_issue:
            status = "rejected_by_stage01"
            reason = "sanitizer_finding_does_not_match_issue"
        elif fixed_required and fixed_checked is not True:
            status = "rejected_by_stage01"
            reason = "fixed_oracle_not_checked"
        elif fixed_required and fixed_acceptable is not True:
            status = "rejected_by_stage01"
            reason = "fixed_oracle_not_clean"
        elif fixed_required and masked_setup:
            status = "rejected_by_stage01"
            reason = "setup_command_masks_build_failures"
        elif not str(result_dir.name).startswith("arvo_") and not (
            isinstance(portability, dict)
            and portability.get("runtime_portable") is True
            and portability.get("clean_replay_ok") is True
        ):
            status = "rejected_by_stage01"
            reason = "runtime_portability_not_established"
        else:
            status = "accepted_for_gt"
            reason = "vulnerable_crash_and_fixed_oracle_confirmed" if fixed_required else "vulnerable_crash_confirmed"
            accepted = True

    screening = {
        "sample_id": result_dir.name,
        "status": status,
        "accepted_for_gt": accepted,
        "reason": reason,
        "fixed_oracle_required": fixed_required,
        "runner_returncode": runner_returncode,
        "reproduction_report": str(report_path),
        "portability_report": str(portability_path),
    }
    return screening


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def publish_stage01_migration(work_dir: Path, published_dir: Path) -> dict[str, Any]:
    """Atomically merge a clean Stage 00-01 portability proof into full GT."""
    from gt_generation.gt_toolkit.evidence import COMMITMENT_FILES, write_commitment
    from gt_generation.gt_toolkit.package_audit import audit_package
    from gt_generation.gt_toolkit.portability import (
        LEGACY_RUNTIME_ARCHIVE_NAMES,
        portability_gate_passes,
    )

    if not published_dir.is_dir():
        return {"published": False, "reason": "published GT directory is missing"}
    if not portability_gate_passes(work_dir):
        return {"published": False, "reason": "portability proof did not pass"}
    for immutable in ("sample_info.json", "poc"):
        source = work_dir / immutable
        target = published_dir / immutable
        if not source.is_file() or not target.is_file():
            return {"published": False, "reason": f"missing immutable {immutable}"}
        if _sha256_file(source) != _sha256_file(target):
            return {
                "published": False,
                "reason": f"Stage 00-01 changed immutable {immutable}",
            }

    protected_names = [
        name for name in (*COMMITMENT_FILES, "context_trace.json")
        if (published_dir / name).is_file()
    ]
    protected_hashes = {
        name: _sha256_file(published_dir / name) for name in protected_names
    }
    try:
        materials = _load_json_or_none(work_dir / "runtime_materials.json")
        entries = materials.get("files") if isinstance(materials, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError("runtime_materials.json has no files")
        staging = published_dir.with_name(published_dir.name + ".portability-staging")
        _remove_path(staging)
        copied = subprocess.run(
            ["cp", "--reflink=auto", "-a", str(published_dir), str(staging)],
            capture_output=True, text=True, errors="replace",
        )
        if copied.returncode != 0:
            shutil.copytree(published_dir, staging, symlinks=True)

        material_paths: list[Path] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"runtime material {index} is not an object")
            relative = Path(str(entry.get("path") or ""))
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise ValueError(f"unsafe runtime material path: {relative}")
            source = work_dir / relative
            if not source.is_file() or _sha256_file(source) != entry.get("sha256"):
                raise ValueError(f"runtime material hash mismatch: {relative}")
            material_paths.append(relative)

        # Remove each referenced helper root first so stale files cannot survive
        # beside the exact manifest contents. Base files are replaced directly.
        base_files = {
            "sample_info.json", "build.sh", "poc",
            "runtime_build.json", "runtime_spec.json",
        }
        helper_roots = {
            relative.parts[0] for relative in material_paths
            if relative.parts[0] not in base_files
        }
        for root in helper_roots:
            _remove_path(staging / root)
        for relative in material_paths:
            source = work_dir / relative
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        for name in (
            "runtime_materials.json", "portability_report.json",
            "reproduction_report.json",
        ):
            source = work_dir / name
            if source.is_file():
                shutil.copy2(source, staging / name)

        for name in (*LEGACY_RUNTIME_ARCHIVE_NAMES, "runtime_work_manifest.json"):
            _remove_path(staging / name)
            for part in staging.glob(name + ".part-*"):
                _remove_path(part)
        for name, expected in protected_hashes.items():
            if _sha256_file(staging / name) != expected:
                raise ValueError(f"semantic evidence changed during migration: {name}")

        write_commitment(staging)
        audit = audit_package(staging)
        if not audit.get("ok") or audit.get("warnings"):
            raise ValueError(
                "migrated package audit failed: "
                + "; ".join([*(audit.get("errors") or []), *(audit.get("warnings") or [])])
            )
        from gt_generation.runner import publish_repair_staging

        publish_repair_staging(staging, published_dir)
        return {
            "published": True,
            "material_files": len(material_paths),
            "protected_files": len(protected_hashes),
        }
    except Exception as exc:
        return {"published": False, "reason": f"{type(exc).__name__}: {exc}"}


def audit_completed_package(result_dir: Path) -> bool:
    audit = subprocess.run(
        [sys.executable, "-m", "gt_toolkit", "audit-package", "--result-dir", str(result_dir)],
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(CODE_ROOT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return audit.returncode == 0


def _command_output(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return (completed.stdout or completed.stderr).strip() or "unknown"


def write_provenance(result_dir: Path, cfg: dict[str, Any], track: str) -> None:
    """Persist the exact agent/Docker configuration used for this sample."""
    adapter = Path(cfg["adapter"])
    data = {
        "schema_version": "gt-generation-provenance-v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "cli": cfg["cli"],
        "model": cfg["model"],
        "reasoning_effort": cfg["reasoning_effort"],
        "strict_config": cfg["strict_config"],
        "adapter": str(adapter),
        "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
        "docker_track": track,
        "repo_docker_image": cfg["repo_docker_image"],
        "repo_docker_context": str(cfg["repo_docker_context"]),
        "evidence_commitment_required": True,
    }
    if cfg.get("codex_provider"):
        provider = dict(cfg["codex_provider"])
        data["codex_provider"] = {
            key: value for key, value in provider.items()
            if key != "env_key"
        }
        if provider.get("env_key"):
            data["codex_provider"]["env_key"] = provider["env_key"]
    if cfg["cli"] == "codex":
        data["cli_version"] = _command_output(["codex", "--version"])
        data["authentication"] = _command_output(["codex", "login", "status"])
    (result_dir / "generation_provenance.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Run                                                                          #
# --------------------------------------------------------------------------- #
def run_one(sample_id: str, sample: dict[str, Any], cfg: dict[str, Any],
            inputs_dir: Path, logs_dir: Path, running: dict[str, float],
            running_lock: threading.Lock) -> dict[str, Any]:
    input_path = inputs_dir / f"{sample_id}.json"
    input_path.write_text(json.dumps(sample, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    published_result_dir = REPO_ROOT / "gt_results" / sample_id
    result_dir = (
        inputs_dir.parent / "results" / sample_id
        if cfg.get("stage01_migration")
        else published_result_dir
    )
    log_path = logs_dir / f"{sample_id}.log"
    track = docker_track(sample)
    if cfg.get("stage01_migration"):
        _remove_path(result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    provenance_dir = result_dir
    if cfg.get("start_at") and all(
        (result_dir / name).is_file()
        for name in (
            "ground_truth.json",
            "verified_assertions.json",
            "verified_invariants.json",
            "assertion_results.json",
        )
    ):
        provenance_dir = inputs_dir / f"{sample_id}.provenance"
        provenance_dir.mkdir(parents=True, exist_ok=True)
    write_provenance(provenance_dir, cfg, track)

    started = time.monotonic()
    with running_lock:
        running[sample_id] = started

    env = {
        **os.environ,
        # runner.py imports gt_toolkit from the repo root after all stages pass.
        "PYTHONPATH": os.pathsep.join(
            [str(REPO_ROOT), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
        ),
        "GT_AGENT_COMMAND": str(cfg["adapter"]),
        "GT_AGENT_MODEL": cfg["model"],
        "GT_AGENT_REASONING_EFFORT": cfg["reasoning_effort"],
        "GT_CODEX_STRICT_CONFIG": "1" if cfg["strict_config"] else "0",
        "GT_REPO_DOCKER_IMAGE": cfg["repo_docker_image"],
        "GT_REPO_DOCKER_CONTEXT": str(cfg["repo_docker_context"]),
        "GT_GENERATION_PROVENANCE_SOURCE": str(
            provenance_dir / "generation_provenance.json"
        ),
    }
    if cfg.get("codex_provider"):
        provider = cfg["codex_provider"]
        env.update({
            "GT_CODEX_PROVIDER_ID": provider["id"],
            "GT_CODEX_PROVIDER_NAME": provider["name"],
            "GT_CODEX_PROVIDER_BASE_URL": provider["base_url"],
            "GT_CODEX_PROVIDER_WIRE_API": provider["wire_api"],
        })
        if provider.get("env_key"):
            env["GT_CODEX_PROVIDER_ENV_KEY"] = provider["env_key"]
        bridge = provider.get("bridge") or {}
        if bridge:
            env.update({
                "GT_CODEX_PROVIDER_BRIDGE": "modelhub_crawl",
                "GT_CODEX_PROVIDER_BRIDGE_TARGET_URL": bridge["target_url"],
                "GT_CODEX_PROVIDER_BRIDGE_MAX_TOKENS": bridge["max_tokens"],
                "GT_CODEX_PROVIDER_BRIDGE_TIMEOUT_SECONDS": bridge["timeout_seconds"],
            })
    command = [
        sys.executable, str(RUNNER),
        "--sample", str(input_path),
        "--result-dir", str(result_dir),
    ]
    # Resume re-runs only the stages that have not passed yet.
    if cfg.get("resume"):
        command.append("--resume")
    if cfg.get("reuse_repair_staging"):
        command.append("--reuse-repair-staging")
    if cfg.get("start_at"):
        command += ["--start-at", str(cfg["start_at"])]
    if cfg.get("stop_after"):
        command += ["--stop-after", str(cfg["stop_after"])]
    with log_path.open("w", encoding="utf-8") as stream:
        completed = subprocess.run(command, cwd=REPO_ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)

    if track == "arvo":
        cleanup_arvo(sample_id)

    with running_lock:
        running.pop(sample_id, None)

    screening: dict[str, Any] | None = None
    migration: dict[str, Any] | None = None
    audit_ok = False
    if completed.returncode == 0:
        if cfg["run_mode"] == "stage01_screening":
            screening = evaluate_stage01_screening(result_dir, sample, completed.returncode)
            if screening.get("accepted_for_gt") and cfg.get("stage01_migration"):
                migration = publish_stage01_migration(result_dir, published_result_dir)
        else:
            audit_ok = audit_completed_package(result_dir)
    elif cfg["run_mode"] == "stage01_screening":
        screening = evaluate_stage01_screening(result_dir, sample, completed.returncode)

    succeeded = (
        bool(screening and screening.get("accepted_for_gt"))
        if cfg["run_mode"] == "stage01_screening"
        else completed.returncode == 0 and audit_ok
    )
    if cfg.get("stage01_migration"):
        succeeded = succeeded and bool(migration and migration.get("published"))
        if succeeded:
            _remove_path(result_dir)

    result = {
        "sample_id": sample_id,
        "track": track,
        "project": sample.get("project"),
        "returncode": completed.returncode,
        "run_mode": cfg["run_mode"],
        "succeeded": succeeded,
        "audit_ok": audit_ok,
        "stage01_screening": screening,
        "stage01_migration": migration,
        "duration_seconds": round(time.monotonic() - started, 3),
        "result_dir": str(published_result_dir),
        "log": str(log_path),
    }
    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CODE_ROOT / "gt_config.json",
                        help="Launcher config JSON (see gt_config.example.json).")
    parser.add_argument("--batch-name", default=datetime.now().strftime("gt_%Y%m%d_%H%M%S"))
    args = parser.parse_args(argv)

    if not args.config.is_file():
        raise SystemExit(f"config not found: {args.config} (copy gt_config.example.json to get started)")
    cfg = load_config(args.config)
    selection = load_selection(cfg["selection_path"])

    missing = [s for s in cfg["samples"] if s not in selection]
    if missing:
        raise SystemExit(f"sample ids absent from selection {cfg['selection_path']}: {missing}")

    # Refresh GT_STATUS.md and skip samples already complete, so a collaborator
    # never re-runs finished work (and can see the up-to-date coverage doc).
    import gt_status
    gt_status.write_status_doc(
        gt_status.scan(gt_status._load_sample_ids(cfg["selection_path"])),
        len(selection), cfg["selection_path"],
    )
    already_done = completed_samples_to_skip(cfg["samples"], cfg)
    if already_done:
        print(f"skipping {len(already_done)} already-complete sample(s): {sorted(already_done)}", flush=True)
    cfg["samples"] = [s for s in cfg["samples"] if s not in already_done]
    if not cfg["samples"]:
        print("nothing to run -- all requested samples are already complete. See GT_STATUS.md", flush=True)
        return 0

    batch_dir = Path("/tmp") / f"gt_batch_{args.batch_name}"
    inputs_dir = batch_dir / "inputs"
    logs_dir = batch_dir / "logs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Routing plan: make the ARVO-vs-repo Docker decision visible before running.
    plan = {s: docker_track(selection[s]) for s in cfg["samples"]}
    print(json.dumps({
        "batch": args.batch_name,
        "cli": cfg["cli"],
        "model": cfg["model"],
        "reasoning_effort": cfg["reasoning_effort"],
        "strict_config": cfg["strict_config"],
        "run_mode": cfg["run_mode"],
        "codex_provider": (
            {key: value for key, value in cfg["codex_provider"].items() if key != "env_key"}
            if cfg.get("codex_provider") else None
        ),
        "repo_docker_image": cfg["repo_docker_image"],
        "repo_docker_context": str(cfg["repo_docker_context"]),
        "parallel_dockers": cfg["parallel_dockers"],
        "samples": len(cfg["samples"]),
        "docker_routing": {
            "arvo (n132/arvo images)": sorted(s for s, t in plan.items() if t == "arvo"),
            "repo (gt-memory-env)": sorted(s for s, t in plan.items() if t == "repo"),
        },
        "batch_dir": str(batch_dir),
    }, indent=2, ensure_ascii=False), flush=True)

    running: dict[str, float] = {}
    running_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=cfg["parallel_dockers"]) as executor:
        futures = {
            executor.submit(run_one, s, selection[s], cfg, inputs_dir, logs_dir, running, running_lock)
            for s in cfg["samples"]
        }
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=30)
            for future in done:
                results.append(future.result())
            if pending:
                with running_lock:
                    active = {
                        s: {"elapsed_seconds": round(time.monotonic() - t, 1),
                            "stage": current_stage(REPO_ROOT / "gt_results" / s)}
                        for s, t in running.items()
                    }
                print("HEARTBEAT " + json.dumps(active, ensure_ascii=False), flush=True)

    results.sort(key=lambda item: cfg["samples"].index(item["sample_id"]))
    summary = {
        "batch": args.batch_name,
        "cli": cfg["cli"],
        "model": cfg["model"],
        "codex_provider": (
            {key: value for key, value in cfg["codex_provider"].items() if key != "env_key"}
            if cfg.get("codex_provider") else None
        ),
        "parallel_dockers": cfg["parallel_dockers"],
        "run_mode": cfg["run_mode"],
        "requested": len(cfg["samples"]),
        "succeeded": sum(1 for r in results if r.get("succeeded")),
        "accepted_for_gt": (
            sum(1 for r in results if (r.get("stage01_screening") or {}).get("accepted_for_gt"))
            if cfg["run_mode"] == "stage01_screening" else None
        ),
        "results": results,
    }
    summary_path = REPO_ROOT / "gt_results" / f"batch_{args.batch_name}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["succeeded"] == len(cfg["samples"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
