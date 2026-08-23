#!/usr/bin/env python3
"""Run OpenHands skill-adapter evaluations under reward_framework/harness_runs.

This entrypoint is intentionally separate from
`poc_generation/poc_generator/rerun_model_batches.py`, whose result namespace is
`poc_generation/poc_results`.  The reward-framework adapter may reuse
`run_sample.py` as the low-level harness executor, but all outputs, logs, status
files, and manifests are owned by `reward_framework/harness_runs/<run_id>/`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
POC_GENERATOR = REPO_ROOT / "poc_generation" / "poc_generator"
RUN_SAMPLE = POC_GENERATOR / "run_sample.py"
RUN_LOCAL_SAMPLE = POC_GENERATOR / "run_local_sample.py"
DEFAULT_RUNS_ROOT = REPO_ROOT / "reward_framework" / "harness_runs"
REPO_PYTHON = REPO_ROOT / "external" / "OpenHands" / ".venv-openhands" / "bin" / "python"
RUNNER_PYTHON = str(
    Path(
        os.getenv("REWARD_FRAMEWORK_RUNNER_PYTHON")
        or os.getenv("OPENHANDS_PYTHON")
        or (str(REPO_PYTHON) if REPO_PYTHON.exists() else sys.executable)
    ).expanduser()
)
PINNED_OPENHANDS_COMMIT = "35b381f3a8f4b5229934515e9f6b479d6d6415ef"

for _path in (REPO_ROOT, REPO_ROOT / "evaluator", REPO_ROOT / "external" / "cybergym" / "src"):
    value = str(_path)
    if value not in sys.path:
        sys.path.insert(0, value)


def _now_id() -> str:
    return time.strftime("openhands_skill_%Y%m%d_%H%M%S")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_samples(args: argparse.Namespace) -> list[str]:
    samples: list[str] = []
    for sample in args.sample or []:
        value = sample.strip()
        if value:
            samples.append(value)
    if args.samples_file:
        path = Path(args.samples_file).expanduser().resolve()
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("["):
            values = json.loads(text)
            samples.extend(str(v).strip() for v in values if str(v).strip())
        else:
            samples.extend(line.strip() for line in text.splitlines() if line.strip())
    if args.start_index:
        samples = samples[args.start_index :]
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        raise ValueError("no samples selected; use --sample or --samples-file")
    return samples


def sample_environment(config: dict[str, Any], *, skill_packet: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CYBERGYM_OPENHANDS_SKILL_PACKET_DIR"] = str(skill_packet)
    runtime_network = str(config.get("openhands_runtime_docker_network") or "").strip()
    if runtime_network:
        env["OPENHANDS_RUNTIME_DOCKER_NETWORK"] = runtime_network
    runtime_extra_hosts = str(config.get("openhands_runtime_extra_hosts") or "").strip()
    if runtime_extra_hosts:
        env["OPENHANDS_RUNTIME_EXTRA_HOSTS"] = runtime_extra_hosts
    if config.get("openhands_runtime_disable_dns"):
        env["OPENHANDS_RUNTIME_DISABLE_DNS"] = "1"
    prompt_file = str(config.get("openhands_prompt_file") or "").strip()
    if prompt_file:
        env["CYBERGYM_OPENHANDS_PROMPT_FILE"] = prompt_file
    session_prefix = str(config.get("openhands_session_prefix") or "").strip()
    if session_prefix:
        env["OPENHANDS_SESSION_PREFIX"] = session_prefix
    max_effective_submits = str(
        config.get("max_effective_submits")
        or config.get("submit_budget")
        or config.get("max_attempts")
        or ""
    ).strip()
    if max_effective_submits:
        env["CYBERGYM_MAX_EFFECTIVE_SUBMITS"] = max_effective_submits
    native_tool_calling = config.get("openhands_native_tool_calling")
    if native_tool_calling is not None:
        if isinstance(native_tool_calling, bool):
            env["OPENHANDS_NATIVE_TOOL_CALLING"] = "true" if native_tool_calling else "false"
        else:
            env["OPENHANDS_NATIVE_TOOL_CALLING"] = str(native_tool_calling).strip()
    return env


def _git_stdout(args: list[str], *, cwd: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    except Exception:
        return None


def _is_clean_openhands_checkout(path: Path) -> bool:
    if not (path / "pyproject.toml").is_file():
        return False
    revision = _git_stdout(["rev-parse", "HEAD"], cwd=path)
    if revision != PINNED_OPENHANDS_COMMIT:
        return False
    status = _git_stdout(["status", "--porcelain"], cwd=path)
    if status is None:
        return False
    dirty = [line for line in status.splitlines() if line.strip() and line.strip() != "?? uv.lock"]
    return not dirty


def resolve_openhands_repo(config: dict[str, Any]) -> Path:
    candidates: list[Path] = []
    raw = str(config.get("openhands_repo") or "").strip()
    if raw:
        candidates.append(Path(raw).expanduser())
    env_raw = os.getenv("REWARD_FRAMEWORK_OPENHANDS_REPO", "").strip()
    if env_raw:
        candidates.append(Path(env_raw).expanduser())
    candidates.extend([
        Path("/tmp/openhands-poc-clean-35b381f3"),
        REPO_ROOT / "external" / "OpenHands",
    ])
    seen: set[str] = set()
    checked: list[str] = []
    for candidate in candidates:
        path = candidate.resolve()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        checked.append(key)
        if _is_clean_openhands_checkout(path):
            return path
    raise RuntimeError(
        "no pinned clean OpenHands checkout found; checked: " + ", ".join(checked)
    )


def build_command(
    config: dict[str, Any],
    sample_id: str,
    *,
    results_dir: Path,
) -> list[str]:
    is_arvo = sample_id.startswith("arvo_")
    common = [
        "--max-iter",
        str(config.get("max_iter", 100)),
        "--timeout",
        str(config.get("timeout", 10800)),
        "--model",
        str(config["model"]),
        "--openhands-repo",
        str(resolve_openhands_repo(config)),
        "--base-url",
        str(config.get("base_url", "")),
        "--api-key-env",
        str(config.get("api_key_env", "OPENAI_API_KEY")),
        "--results-dir",
        str(results_dir),
    ]
    api_version = str(config.get("api_version") or "").strip()
    if api_version:
        common.extend(["--api-version", api_version])
    if is_arvo:
        return [
            RUNNER_PYTHON,
            str(RUN_SAMPLE),
            "--arvo-id",
            sample_id[len("arvo_") :] if sample_id.startswith("arvo_") else sample_id,
            "--server",
            str(config.get("server", "http://host.docker.internal:8666")),
            "--difficulty",
            str(config.get("difficulty", "level1")),
            "--max-attempts",
            str(config.get("generation_attempts", config.get("max_attempts", 1))),
            "--harness-profile",
            str(config.get("harness_profile") or "baseline"),
            *common,
        ]
    return [
        RUNNER_PYTHON,
        str(RUN_LOCAL_SAMPLE),
        "--sample-id",
        sample_id,
        *common,
    ]


def maybe_run_reachability(
    config: dict[str, Any],
    *,
    run_id: str,
    sample_id: str,
    sample_dir: Path,
) -> dict[str, Any]:
    if not config.get("run_reachability_after_generation"):
        return {"status": "disabled"}
    try:
        from evaluator.reachability.eval_batch import evaluate_model_sample

        result = evaluate_model_sample(
            model=run_id,
            sample_id=sample_id,
            sample_dir=sample_dir,
            timeout=int(config.get("reachability_timeout", 420)),
            debugger_image=str(
                config.get("reachability_debugger_image") or "gt-memory-env:latest"
            ),
            max_hits_per_event=int(config.get("reachability_max_hits_per_event", 64)),
        )
    except Exception as exc:  # noqa: BLE001 - generation result remains useful.
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    if "skipped" in result:
        return {"status": "skipped", "reason": result["skipped"]}
    if "error" in result:
        return {"status": "error", "error": result["error"]}
    return {"status": "ok", "summary": result.get("summary") or {}}


def run_one(
    config: dict[str, Any],
    sample_id: str,
    *,
    run_id: str,
    run_dir: Path,
    skill_packet: Path,
    overwrite: bool,
) -> dict[str, Any]:
    results_dir = run_dir / "results"
    sample_dir = results_dir / sample_id
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    if sample_dir.exists() and (sample_dir / "manifest.json").is_file() and not overwrite:
        return {"sample": sample_id, "status": "skipped_existing", "sample_dir": str(sample_dir)}

    env = sample_environment(config, skill_packet=skill_packet)
    command = build_command(config, sample_id, results_dir=results_dir)
    log_path = logs_dir / f"{sample_id}.log"
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n")
        log.flush()
        proc = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=int(config.get("sample_timeout", config.get("timeout", 10800))),
            check=False,
        )
    status = "complete" if proc.returncode == 0 else "failed"
    record: dict[str, Any] = {
        "sample": sample_id,
        "status": status,
        "returncode": proc.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "sample_dir": str(sample_dir),
        "log": str(log_path),
    }
    if sample_dir.is_dir() and (sample_dir / "manifest.json").is_file():
        record["reachability"] = maybe_run_reachability(
            config,
            run_id=run_id,
            sample_id=sample_id,
            sample_dir=sample_dir,
        )
    else:
        record["reachability"] = {"status": "skipped", "reason": "missing manifest"}
    return record


def write_manifest(
    run_dir: Path,
    *,
    run_id: str,
    config_path: Path,
    config: dict[str, Any],
    samples: list[str],
    skill_packet: Path,
) -> None:
    manifest = {
        "run_id": run_id,
        "adapter": "openhands",
        "entrypoint": "reward_framework.adapters.openhands.run_samples",
        "results_dir": str(run_dir / "results"),
        "logs_dir": str(run_dir / "logs"),
        "config_path": str(config_path),
        "model": config.get("model"),
        "base_url": config.get("base_url"),
        "api_key_env": config.get("api_key_env"),
        "skill_packet": str(skill_packet),
        "samples": samples,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Reward-framework adapter run; does not write poc_generation/poc_results.",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    safe_config = dict(config)
    (run_dir / "run_config_effective.json").write_text(
        json.dumps(safe_config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="OpenHands/model config JSON.")
    ap.add_argument("--sample", action="append", default=[])
    ap.add_argument("--samples-file")
    ap.add_argument("--start-index", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    ap.add_argument("--skill-packet", default="")
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Write manifest and planned commands without launching OpenHands.",
    )
    args = ap.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = _load_json(config_path)
    samples = load_samples(args)
    run_id = args.run_id or _now_id()
    run_dir = args.runs_root.expanduser().resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    skill_packet = Path(
        args.skill_packet or config.get("openhands_skill_packet_dir") or ""
    ).expanduser().resolve()
    if not skill_packet.is_dir():
        raise FileNotFoundError(
            "skill packet is required for reward-framework OpenHands runs: "
            f"{skill_packet}"
        )
    write_manifest(
        run_dir,
        run_id=run_id,
        config_path=config_path,
        config=config,
        samples=samples,
        skill_packet=skill_packet,
    )
    if args.dry_run:
        plans = [
            {
                "sample": sample,
                "command": build_command(config, sample, results_dir=run_dir / "results"),
                "results_dir": str(run_dir / "results"),
                "sample_dir": str(run_dir / "results" / sample),
            }
            for sample in samples
        ]
        (run_dir / "planned_commands.json").write_text(
            json.dumps(plans, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        summary = {
            "run_id": run_id,
            "adapter": "openhands",
            "dry_run": True,
            "planned": len(plans),
            "results_dir": str(run_dir / "results"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    status_path = run_dir / "status.jsonl"
    summary_path = run_dir / "summary.json"
    counts: dict[str, int] = {}
    parallel = max(1, int(args.parallel))
    if parallel > 2:
        raise ValueError("refusing to run more than 2 OpenHands samples in parallel")
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {
            executor.submit(
                run_one,
                config,
                sample,
                run_id=run_id,
                run_dir=run_dir,
                skill_packet=skill_packet,
                overwrite=args.overwrite,
            ): sample
            for sample in samples
        }
        with status_path.open("a", encoding="utf-8") as status_file:
            for future in as_completed(futures):
                record = future.result()
                records.append(record)
                counts[record["status"]] = counts.get(record["status"], 0) + 1
                status_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                status_file.flush()
                summary = {
                    "run_id": run_id,
                    "adapter": "openhands",
                    "processed": sum(counts.values()),
                    "total": len(samples),
                    "counts": counts,
                    "results_dir": str(run_dir / "results"),
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                summary_path.write_text(
                    json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
    return 0 if not counts.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
