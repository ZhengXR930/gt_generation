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

from reward_framework.adapters import HARNESSES, RewardRequest, build_command
from reward_framework.adapters.base import DEFAULT_RUNS_ROOT, DEFAULT_SKILL_PACKET


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


def load_samples(args: argparse.Namespace, config: dict[str, Any]) -> list[str]:
    samples: list[str] = []
    selector = _config_value(args, config, "sample_selector", "")
    if selector:
        samples.extend(_selected_valid_gt_samples(str(selector)))
    samples.extend(
        str(item).strip() for item in config.get("samples", []) if str(item).strip()
    )
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
    if not payload.get("workspace_adapter"):
        return False
    return (
        isinstance(artifact, dict)
        and artifact.get("sample_id") == sample_dir.name
        and isinstance(artifact.get("fine_trace"), list)
        and isinstance(artifact.get("vuln_logic"), dict)
    )


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


def build_request(
    args: argparse.Namespace,
    config: dict[str, Any],
    *,
    run_id: str,
    run_dir: Path,
    sample_id: str,
) -> RewardRequest:
    model = str(_config_value(args, config, "model"))
    skill_packet = Path(
        str(_config_value(args, config, "skill_packet", str(DEFAULT_SKILL_PACKET)))
    ).expanduser().resolve()
    results_dir = run_dir / "results"
    max_effective = _config_value(args, config, "max_effective_submits")
    return RewardRequest(
        harness=str(_config_value(args, config, "harness", "openhands")),
        sample_id=sample_id,
        model=model,
        run_id=run_id,
        results_dir=results_dir,
        skill_packet=skill_packet,
        base_url=str(_config_value(args, config, "base_url", "")),
        api_key_env=str(_config_value(args, config, "api_key_env", "")),
        api_version=str(_config_value(args, config, "api_version", "")),
        max_iter=int(_config_value(args, config, "max_iter", 100)),
        max_attempts=int(
            config.get("generation_attempts")
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
        reasoning_effort=str(_config_value(args, config, "reasoning_effort", "max")),
        max_output_tokens=int(_config_value(args, config, "max_output_tokens", 4096)),
        extra_args=tuple(str(item) for item in config.get("extra_args", [])),
    )


def run_one(
    args: argparse.Namespace,
    config: dict[str, Any],
    *,
    run_id: str,
    run_dir: Path,
    sample_id: str,
) -> dict[str, Any]:
    request = build_request(args, config, run_id=run_id, run_dir=run_dir, sample_id=sample_id)
    if not request.skill_packet.is_dir():
        raise FileNotFoundError(f"skill packet not found: {request.skill_packet}")
    command = build_command(request)
    sample_dir = request.results_dir / sample_id
    if sample_dir.exists() and not args.overwrite and result_is_complete(sample_dir):
        record: dict[str, Any] = {
            "sample": sample_id,
            "status": "skipped",
            "results_dir": str(sample_dir),
        }
        if not (sample_dir / "reachability_eval.json").is_file():
            record["reachability"] = maybe_run_reachability(
                config, run_id=run_id, sample_id=sample_id, sample_dir=sample_dir
            )
        return record
    if args.dry_run:
        return {"sample": sample_id, "status": "planned", "command": command.redacted()}

    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    request.results_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{sample_id}.log"
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
            config, run_id=run_id, sample_id=sample_id, sample_dir=sample_dir
        )
    return record


def write_run_manifest(
    run_dir: Path,
    *,
    run_id: str,
    config_path: Path | None,
    config: dict[str, Any],
    samples: list[str],
    args: argparse.Namespace,
) -> None:
    harness = str(_config_value(args, config, "harness", "openhands"))
    skill_packet = Path(
        str(_config_value(args, config, "skill_packet", str(DEFAULT_SKILL_PACKET)))
    ).expanduser().resolve()
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
        "skill_packet": str(skill_packet),
        "max_effective_submits": _config_value(args, config, "max_effective_submits"),
        "samples": samples,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "run_config_effective.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--samples-file")
    parser.add_argument("--sample-selector")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--skill-packet")
    parser.add_argument("--harness", choices=HARNESSES)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env")
    parser.add_argument("--api-version")
    parser.add_argument("--max-iter", type=int)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-effective-submits", type=int)
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--max-output-tokens", type=int)
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
    write_run_manifest(
        run_dir,
        run_id=run_id,
        config_path=config_path,
        config=config,
        samples=samples,
        args=args,
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
                record = future.result()
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
