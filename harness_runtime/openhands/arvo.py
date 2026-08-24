#!/usr/bin/env python3
"""Drive one CyberGym+OpenHands run for a single GT sample.

Runs the actual generation attempt (tools + real sandbox repo) in a throwaway
scratch dir, then copies only the durable checkpoint pieces (file_store,
trajectory, config.toml, args.json -- NOT the extracted repo/workspace, which
can be 1-2GB and is not part of the durable session state) into
the caller's result namespace.
The subject's final analysis artifact is captured as part of the same task.
"""
import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import docker

BACKEND_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = BACKEND_ROOT.parent
GT_ROOT = RUNTIME_ROOT.parent
sys.path.insert(0, str(RUNTIME_ROOT))
sys.path.insert(0, str(GT_ROOT))
sys.path.insert(0, str(GT_ROOT / "external" / "cybergym" / "src"))

from harness_runtime.openhands.runtime import (  # noqa: E402
    configure_harness_profile,
    run_with_configs,
    OpenhandsArgs,
    LLMArgs,
    TaskArgs,
)
from cybergym.task.types import TaskDifficulty  # noqa: E402
from harness_runtime.submission_db import check as check_success  # noqa: E402
from harness_runtime.dedup import deduplicate_submission_attempts  # noqa: E402
from evaluator.reasoning.analysis_artifact import validate_analysis_artifact_quality  # noqa: E402


def _local_docker_chown_images():
    """Return local-only images suitable for chown fallback cleanup.

    Do not ask Docker to pull missing images here. Scratch cleanup must not turn
    a finished benchmark run into an external Docker Hub dependency.
    """
    candidates = [
        os.getenv("GT_CLEANUP_CHOWN_IMAGE"),
        os.getenv("OPENHANDS_RUNTIME_CONTAINER_IMAGE"),
        "gt-memory-env:latest",
        "alpine:latest",
        "alpine:3.17",
        "alpine:3.23",
    ]
    client = docker.from_env()
    available = []
    seen = set()
    for image in candidates:
        if not image or image in seen:
            continue
        seen.add(image)
        try:
            client.images.get(image)
        except Exception:
            continue
        available.append(image)
    return available


def cleanup_scratch(scratch: Path) -> None:
    """Remove an OpenHands scratch tree even when runtime files are root-owned."""
    scratch = scratch.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if scratch.parent != temp_root or not scratch.name.startswith("run_arvo_"):
        logging.warning("Refusing to clean unexpected scratch path: %s", scratch)
        return
    try:
        shutil.rmtree(scratch)
        return
    except PermissionError:
        pass

    last_exc = None
    for image in _local_docker_chown_images():
        try:
            docker.from_env().containers.run(
                image,
                command=["chown", "-R", f"{os.getuid()}:{os.getgid()}", "/scratch"],
                volumes={str(scratch): {"bind": "/scratch", "mode": "rw"}},
                remove=True,
            )
            shutil.rmtree(scratch)
            return
        except Exception as exc:
            last_exc = exc
            logging.warning(
                "Scratch cleanup chown failed with local image %s for %s: %s",
                image,
                scratch,
                exc,
            )
    logging.warning("Could not fully clean scratch %s: %s", scratch, last_exc)


def ensure_arvo_source(arvo_id: str) -> Path:
    """Materialize the source supplied to the subject from the stock ARVO image.

    The lightweight CyberGym metadata subset intentionally does not contain the
    large repo tarballs.  Hydrate each selected sample once, before creating the
    task workspace, so README.md never advertises a source tree that is absent.
    """
    arvo_dir = GT_ROOT / "external" / "cybergym_data_subset" / "data" / "arvo" / arvo_id
    arvo_dir.mkdir(parents=True, exist_ok=True)
    lock_path = arvo_dir / ".hydrate.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return _ensure_arvo_source_locked(arvo_id, arvo_dir)


def normalize_public_issue_description(description: str) -> str:
    return "\n".join(line.rstrip() for line in description.strip().splitlines()).strip()


def load_public_issue_description(sample_id: str) -> str:
    """Load the public natural-language issue description for a benchmark task.

    The curated per-sample package is the authoritative public task input.  It
    is safe for PoC generation because it contains only the issue description,
    not GT traces, sanitizer output, assertions, known PoCs, or crash state.
    ``selected_1000.json`` is kept only as a compatibility fallback.
    """
    curated_path = GT_ROOT / "gt_results" / sample_id / "issue_description.json"
    if curated_path.is_file():
        value = json.loads(curated_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            description = normalize_public_issue_description(
                str(value.get("issue_description") or "")
            )
            if description:
                return description
        raise RuntimeError(f"{sample_id} has invalid issue_description in {curated_path}")

    selected_path = GT_ROOT / "dataset" / "selected_1000.json"
    selected = json.loads(selected_path.read_text(encoding="utf-8"))
    description = next(
        (
            normalize_public_issue_description(str(item.get("issue_description") or ""))
            for item in selected
            if isinstance(item, dict) and item.get("sample_id") == sample_id
        ),
        "",
    )
    if not description:
        raise RuntimeError(
            f"{sample_id} has no issue_description in {curated_path} or {selected_path}"
        )
    return description


def _ensure_arvo_source_locked(arvo_id: str, arvo_dir: Path) -> Path:
    description_path = arvo_dir / "description.txt"
    sample_id = f"arvo_{arvo_id}"
    description = load_public_issue_description(sample_id)
    if (
        not description_path.is_file()
        or description_path.read_text(encoding="utf-8", errors="replace").strip()
        != description
    ):
        description_path.write_text(description + "\n", encoding="utf-8")

    repo_dir = arvo_dir / "repo-vul"
    source_dir = repo_dir / "src-vul"
    if source_dir.is_dir() and any(source_dir.iterdir()):
        return repo_dir

    image = f"n132/arvo:{arvo_id}-vul"
    client = docker.from_env()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        logging.info("Pulling missing target image %s", image)
        client.images.pull(image)

    logging.info("Hydrating %s source from %s", arvo_id, image)
    container = client.containers.create(image)
    staging = Path(tempfile.mkdtemp(prefix=".repo-vul-", dir=arvo_dir))
    try:
        staged_source = staging / "src-vul"
        staged_work = staging / "work-vul"
        staged_source.mkdir()
        staged_work.mkdir()
        subprocess.run(
            ["docker", "cp", f"{container.id}:/src/.", str(staged_source)],
            check=True,
        )
        subprocess.run(
            ["docker", "cp", f"{container.id}:/work/.", str(staged_work)],
            check=True,
        )
        if not any(staged_source.iterdir()):
            raise RuntimeError(f"{image}:/src produced an empty source tree")
        repo_dir.mkdir(exist_ok=True)
        if source_dir.exists() and not any(source_dir.iterdir()):
            source_dir.rmdir()
        staged_source.replace(source_dir)
        if any(staged_work.iterdir()) and not (repo_dir / "work-vul").exists():
            staged_work.replace(repo_dir / "work-vul")
        return repo_dir
    finally:
        try:
            container.remove(force=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def load_env_key(var_name: str) -> str:
    if os.environ.get(var_name):
        return os.environ[var_name]
    cfg = GT_ROOT / "config.txt"
    for line in cfg.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{var_name}="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError(f"{var_name} not found in env or {cfg}")


def default_api_key_env(model: str) -> str:
    normalized = model[len("openai/"):] if model.startswith("openai/") else model
    if normalized.startswith("deepseek"):
        return "DEEPSEEK_API_KEY"
    if normalized.startswith("claude-"):
        return "ANTHROPIC_API_KEY"
    if normalized.startswith(("gpt-", "o3", "o4")):
        return "OPENAI_API_KEY"
    return "LLM_API_KEY"


def native_tool_calling_for_model(model: str) -> bool | None:
    """Override old OpenHands capability tables for newer official models."""
    override = os.getenv("OPENHANDS_NATIVE_TOOL_CALLING", "").strip().lower()
    if override:
        if override in {"1", "true", "yes", "on"}:
            return True
        if override in {"0", "false", "no", "off"}:
            return False
        raise ValueError(
            "OPENHANDS_NATIVE_TOOL_CALLING must be true/false when set; "
            f"got {override!r}"
        )
    normalized = model[len("openai/"):] if model.startswith("openai/") else model
    if normalized.startswith(("gpt-5.4", "gpt-5.5")):
        return True
    return None


def redact_secrets(value):
    """Return a JSON-serializable copy with credential-looking fields redacted."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key.lower() in {"api_key", "apikey", "token", "access_token", "secret"}:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def copy_json_redacted(src: Path, dst: Path) -> None:
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        shutil.copy2(src, dst)
        return
    dst.write_text(json.dumps(redact_secrets(payload), indent=2), encoding="utf-8")


def unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def materialize_attempt_analysis_files(attempt_dir: Path, analysis_name: str = "analysis.json") -> bool:
    """Persist one immutable submission analysis artifact beside its PoC files."""
    analysis_path = attempt_dir / analysis_name
    try:
        raw = analysis_path.read_text(encoding="utf-8")
        artifact = json.loads(raw)
    except (OSError, TypeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"sample_id", "fine_trace", "vuln_logic"}
    ):
        return False
    if validate_analysis_artifact_quality(raw) is not None:
        return False
    (attempt_dir / "analysis.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def runtime_server_url(server: str) -> str:
    """Use the Docker bridge gateway on Linux, where host.docker.internal
    is not guaranteed to be registered inside the OpenHands runtime."""
    if sys.platform.startswith("linux"):
        gateway = os.getenv("OPENHANDS_EVAL_HOST_GATEWAY", "").strip()
        if not gateway:
            try:
                bridge = docker.from_env().networks.get("bridge")
                configs = (bridge.attrs.get("IPAM") or {}).get("Config") or []
                gateway = next(
                    str(item.get("Gateway") or "").strip()
                    for item in configs
                    if str(item.get("Gateway") or "").strip()
                )
            except (docker.errors.DockerException, StopIteration, TypeError):
                gateway = "172.17.0.1"
        return server.replace("host.docker.internal", gateway)
    return server


def find_run_dir(log_dir: Path, task_id_safe: str, agent_id: str | None) -> Path | None:
    if agent_id:
        candidate = log_dir / f"{task_id_safe}-{agent_id}"
        if candidate.exists():
            return candidate
    matches = sorted(log_dir.glob(f"{task_id_safe}-*"), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def persist_submission_attempts(
    sample_dir: Path,
    cybergym_agent_id: str,
    attempts: list[dict],
    fallback_analysis: Path | None = None,
    server_root: Path | None = None,
) -> list[dict]:
    """Copy the immutable server ledger into this model/sample result tree."""
    if server_root is None:
        raise ValueError("server_root is required")
    source_root = server_root / "logs" / "submissions" / cybergym_agent_id
    destination_root = sample_dir / "submissions"
    destination_root.mkdir(parents=True, exist_ok=True)
    persisted = []
    for sequence, attempt in enumerate(attempts, 1):
        attempt_id = str(attempt.get("attempt_id") or "")
        if not attempt_id:
            continue
        source = source_root / attempt_id
        destination = destination_root / attempt_id
        if source.is_dir() and not destination.exists():
            shutil.copytree(source, destination)
        if destination.is_dir():
            materialize_attempt_analysis_files(destination)
            if fallback_analysis is not None and not (destination / "analysis.json").is_file():
                try:
                    shutil.copy2(fallback_analysis, destination / "analysis.json")
                except OSError:
                    pass
        record = dict(attempt)
        record["result_path"] = (
            f"submissions/{attempt_id}/" if destination.is_dir() else None
        )
        record["sequence_in_run"] = sequence
        persisted.append(record)
    return persisted


def clear_previous_result(sample_dir: Path) -> None:
    """Remove the previous result for this exact model/sample before rerunning.

    Model namespaces already isolate DeepSeek from GPT, so retaining another
    per-run archive only duplicates large checkpoints. A rerun is an explicit
    replacement of the prior result.
    """
    for name in ("checkpoint", "submissions", "runs"):
        path = sample_dir / name
        if path.is_dir():
            shutil.rmtree(path)
    for name in (
        "manifest.json",
        "analysis_artifact.json",
        "analysis.json",
        "analysis_artifact.response.txt",
        "fine_trace.json",
        "fine_trace.response.txt",
        "vuln_logic.json",
    ):
        unlink_if_exists(sample_dir / name)


def persist_analysis_artifact(candidate_path: Path, sample_dir: Path) -> bool:
    try:
        raw = candidate_path.read_text(encoding="utf-8")
        artifact = json.loads(raw)
    except (OSError, TypeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(artifact, dict)
        or not isinstance(artifact.get("sample_id"), str)
        or not artifact["sample_id"].strip()
        or not isinstance(artifact.get("fine_trace"), list)
        or not isinstance(artifact.get("vuln_logic"), dict)
    ):
        return False
    if artifact.get("sample_id") != sample_dir.name:
        return False
    if validate_analysis_artifact_quality(raw) is not None:
        return False
    (sample_dir / "analysis.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def count_agent_actions(trajectory_path: Path) -> int:
    """Count actual agent actions rather than inferring budget exhaustion from text."""
    try:
        events = json.loads(trajectory_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return 0
    if not isinstance(events, list):
        return 0
    return sum(
        1
        for event in events
        if isinstance(event, dict)
        and event.get("source") == "agent"
        and isinstance(event.get("action"), str)
    )


def trajectory_has_finish_action(trajectory_path: Path) -> bool:
    """Require an explicit terminal action before calling an early stop clean."""
    try:
        events = json.loads(trajectory_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return False
    return isinstance(events, list) and any(
        isinstance(event, dict)
        and event.get("source") == "agent"
        and event.get("action") == "finish"
        for event in events
    )


def count_jsonl_kind(path: Path | None, kind: str) -> int:
    if path is None or not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("kind") == kind:
            count += 1
    return count


def run_attempt(
    args,
    task_id: str,
    task_id_safe: str,
    sample_id: str,
    results_dir: Path,
) -> str | None:
    """One full generation episode on a fresh scratch + Docker workspace. Copies
    the durable checkpoint pieces and writes the manifest to poc_results/<id>/.
    Returns the run status -- 'success' (PoC crashed), 'iteration_cap' (reached
    the iteration limit), or 'incomplete' (died early, e.g. a stuck loop) -- or
    None if no run dir was produced. Always cleans up its scratch copy.

    Only 'success'/'iteration_cap' reach a normal endpoint with a valid final
    fine trace; 'incomplete' means the episode should be re-run (see main)."""
    requested_profile = getattr(args, "harness_profile", None)
    if requested_profile is not None:
        harness_profile = requested_profile
        configure_harness_profile(
            harness_profile,
            max_iterations=args.max_iter,
        )
        # configure_harness_profile intentionally selects the pristine upstream
        # entrypoint. The remote PoC evaluation still wraps that controller with
        # the lifecycle-only artifact overlay so clean endpoints and iteration
        # caps produce the required analysis.json.
        if os.getenv("OPENHANDS_CAPTURE_FINE_TRACE") == "1":
            os.environ["OPENHANDS_MAIN_MODULE"] = "harness_runtime.openhands.overlay"
    else:
        # Historical experiment drivers install their own isolated entrypoints
        # before calling run_attempt directly. Preserve those callers; the
        # production CLI always supplies an explicit baseline profile.
        harness_profile = os.getenv("OPENHANDS_HARNESS_PROFILE", "legacy_external")
    scratch = Path(tempfile.mkdtemp(prefix=f"run_{sample_id}_"))
    scratch_log_dir = scratch / "results"
    scratch_tmp_dir = scratch / "tmp"
    scratch_log_dir.mkdir()
    scratch_tmp_dir.mkdir()

    openhands_args = OpenhandsArgs(
        log_dir=scratch_log_dir,
        tmp_dir=scratch_tmp_dir,
        llm=LLMArgs(
            model=args.model,
            base_url=args.base_url,
            api_version=getattr(args, "api_version", None) or None,
            api_key=load_env_key(
                args.api_key_env or default_api_key_env(args.model)
            ),
            native_tool_calling=native_tool_calling_for_model(args.model),
        ),
        max_iter=args.max_iter,
        repo=args.openhands_repo.expanduser().resolve(),
        remove_tmp=False,  # need config.toml still present to copy it out below
        timeout=args.timeout,
    )
    task_args = TaskArgs(
        task_id=task_id,
        data_dir=GT_ROOT / "external" / "cybergym_data_subset" / "data",
        server=runtime_server_url(args.server),
        difficulty=TaskDifficulty(args.difficulty),
    )

    try:
        try:
            returned_agent_id = run_with_configs(openhands_args, task_args)
        except Exception as exc:
            logging.exception(
                "run_with_configs raised %r; still attempting checkpoint save "
                "from partial state",
                exc,
            )
            returned_agent_id = None

        run_dir = find_run_dir(openhands_args.log_dir, task_id_safe, returned_agent_id)
        if run_dir is None:
            print(json.dumps({"arvo_id": args.arvo_id, "status": "no_run_dir_found"}, indent=2))
            return None

        args_json = json.loads((run_dir / "args.json").read_text())
        cybergym_agent_id = args_json["task"]["agent_id"]

        sample_dir = results_dir / sample_id
        db_path = args.server_root / "poc.db"
        success_info = (
            check_success(db_path, cybergym_agent_id)
            if db_path.exists() else {"ok": False, "error": "db not found"}
        )
        task_workspace = Path(os.environ.get("OPENHANDS_TASK_WORKSPACE") or "")
        fallback_analysis = task_workspace / ".latest_analysis.json"
        if not fallback_analysis.is_file():
            fallback_analysis = sample_dir / "analysis.json"
        persisted_attempts = persist_submission_attempts(
            sample_dir,
            cybergym_agent_id,
            success_info.get("submission_attempts") or [],
            fallback_analysis if fallback_analysis.is_file() else None,
            args.server_root,
        )
        poc_deduplication, deduplicated_pocs = (
            deduplicate_submission_attempts(persisted_attempts)
        )
        analysis_source = "task_finalization"
        valid_attempts = [
            attempt for attempt in persisted_attempts if attempt.get("analysis_valid")
        ]
        if valid_attempts:
            latest = valid_attempts[-1]
            candidate_path = (
                sample_dir / str(latest["analysis_path"])
                if latest.get("analysis_path")
                else sample_dir / str(latest["result_path"]) / "analysis.json"
            )
            if candidate_path.is_file():
                if persist_analysis_artifact(candidate_path, sample_dir):
                    analysis_source = "last_valid_poc_submission"

        # A final analysis artifact is written ONLY when the episode reaches a clean
        # endpoint (iteration limit / agent finished). Its
        # presence is therefore the reliable signal that the run terminated
        # cleanly -- unlike a trajectory-length heuristic, which a stuck loop
        # inflates past max_iter and so misreports an early death as a genuine
        # iteration cap. No artifact + no success => the episode died early
        # (stuck loop / error) and should be re-run (see main).
        analysis_produced = (results_dir / sample_id / "analysis.json").exists()
        trajectory_path = run_dir / "trajectory"
        agent_action_count = count_agent_actions(trajectory_path)
        terminal_finish_observed = trajectory_has_finish_action(trajectory_path)
        terminal_guard_enabled = os.getenv(
            "SUBMIT_CANDIDATE_TERMINAL_GUARD", "0"
        ) == "1"
        terminal_guard_log_raw = os.getenv("SUBMIT_CANDIDATE_TOOL_LOG", "").strip()
        blocked_finish_count = count_jsonl_kind(
            Path(terminal_guard_log_raw) if terminal_guard_log_raw else None,
            "premature_finish_blocked",
        )
        # A blocked finish is returned as NullAction and consumes one controller
        # iteration, but OpenHands does not persist it as an agent action.  Add it
        # back when determining whether the configured global limit was reached.
        effective_controller_iterations = agent_action_count + blocked_finish_count
        finalization_marker_seen = (
            trajectory_path.is_file()
            and "[Analysis Artifact Finalization]" in trajectory_path.read_text(
                encoding="utf-8", errors="replace"
            )
        )
        reached_iteration_cap = (
            trajectory_path.is_file()
            and (
                (
                    terminal_guard_enabled
                    and (
                        finalization_marker_seen
                        or effective_controller_iterations >= args.max_iter
                    )
                )
                or (
                    not terminal_guard_enabled
                    and agent_action_count >= args.max_iter
                    and finalization_marker_seen
                )
            )
        )
        if success_info.get("success"):
            status = "success"
        elif analysis_produced and persisted_attempts:
            status = "agent_finished"
        elif analysis_produced and reached_iteration_cap:
            status = "iteration_cap"
        elif analysis_produced and terminal_finish_observed:
            status = "agent_finished"
        else:
            status = "incomplete"

        # Copy only the durable checkpoint pieces -- not the extracted repo/workspace.
        # When a separate trace-finalization turn ran, promote the snapshot taken
        # before that turn. The finalization events must not alter the resumable
        # tool-using checkpoint.
        checkpoint_dir = sample_dir / "checkpoint"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        pre_finalization_dir = checkpoint_dir / "pre_finalization"
        frozen_checkpoint = (
            pre_finalization_dir
            if (pre_finalization_dir / "metadata.json").is_file()
            else None
        )
        tmp_input_dir = openhands_args.tmp_dir / run_dir.name

        for name in ("file", "cache"):
            src = (frozen_checkpoint or run_dir) / name
            dst = checkpoint_dir / name
            if dst.exists():
                shutil.rmtree(dst)
            if src.exists():
                shutil.copytree(src, dst)
            else:
                dst.mkdir()
        for name in ("trajectory", "args.json"):
            src = (
                frozen_checkpoint / name
                if frozen_checkpoint is not None and name == "trajectory"
                else run_dir / name
            )
            if src.exists():
                if name == "args.json":
                    copy_json_redacted(src, checkpoint_dir / name)
                else:
                    shutil.copy2(src, checkpoint_dir / name)
        for name in ("config.toml", "prompt.txt"):
            src = tmp_input_dir / "template" / name
            if src.exists():
                shutil.copy2(src, checkpoint_dir / name)
        if frozen_checkpoint is not None:
            shutil.copy2(
                frozen_checkpoint / "metadata.json",
                checkpoint_dir / "metadata.json",
            )
            shutil.rmtree(frozen_checkpoint)

        manifest_entry = {
            "evaluation_protocol": "poc_analysis_artifact_per_submission_v3",
            "arvo_id": args.arvo_id,
            "task_id": task_id,
            "sample_id": sample_id,
            "session_name": args_json["session_name"],
            "cybergym_agent_id": cybergym_agent_id,
            "model": args.model,
            "harness_profile": harness_profile,
            "base_url": args.base_url,
            "api_version": getattr(args, "api_version", ""),
            "api_key_env": args.api_key_env or default_api_key_env(args.model),
            "max_iter": args.max_iter,
            "workspace_adapter": args_json.get("workspace_adapter"),
            "agent_action_count": agent_action_count,
            "blocked_premature_finish_count": blocked_finish_count,
            "effective_controller_iterations": effective_controller_iterations,
            "terminal_finish_observed": terminal_finish_observed,
            "status": status,
            "poc_generation": success_info,
            "submission_attempts": persisted_attempts,
            "poc_deduplication": poc_deduplication,
            "deduplicated_pocs": deduplicated_pocs,
            "analysis": {
                "path": "analysis.json",
                "produced": (sample_dir / "analysis.json").is_file(),
                "format": "JSON object with sample_id, fine_trace, and vuln_logic",
                "source": analysis_source,
            },
            "checkpoint": {
                "dir": "checkpoint/",
                "phase": (
                    "pre_analysis_artifact_finalization"
                    if frozen_checkpoint is not None
                    else "terminal"
                ),
                "note": (
                    "workspace/ is intentionally NOT persisted here (the extracted repo "
                    "can be 1-2GB and the durable session checkpoint does not require it). "
                    "Re-materialize "
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
        cleanup_scratch(scratch)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arvo-id", required=True, help="numeric ARVO id, e.g. 1304")
    ap.add_argument("--max-iter", type=int, default=100)
    ap.add_argument("--server", default="http://host.docker.internal:8666")
    ap.add_argument("--difficulty", default="level1")
    ap.add_argument("--timeout", type=int, default=10800)
    ap.add_argument("--model", default="deepseek/deepseek-chat")
    ap.add_argument(
        "--harness-profile",
        choices=("standard",),
        default="standard",
    )
    ap.add_argument(
        "--openhands-repo",
        type=Path,
        default=GT_ROOT / "external" / "OpenHands",
        help="Complete OpenHands checkout containing pyproject.toml.",
    )
    ap.add_argument(
        "--base-url",
        default="",
        help="Provider API base URL.",
    )
    ap.add_argument(
        "--api-version",
        default="",
        help="Optional API version for Azure-compatible endpoints.",
    )
    ap.add_argument(
        "--api-key-env",
        default="",
        help="Environment/config.txt variable containing the API key.",
    )
    ap.add_argument(
        "--results-dir",
        type=Path,
        required=True,
    )
    ap.add_argument("--prompt-file", required=True, type=Path)
    ap.add_argument("--server-root", required=True, type=Path)
    ap.add_argument("--workspace-installer", default="")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="Re-run the whole episode up to this many times if it dies early "
                         "(stuck loop / no normal endpoint), since every completed task "
                         "must yield a final analysis artifact.")
    args = ap.parse_args()
    args.prompt_file = args.prompt_file.expanduser().resolve()
    args.server_root = args.server_root.expanduser().resolve()
    if not args.prompt_file.is_file():
        ap.error(f"prompt file not found: {args.prompt_file}")
    os.environ["HARNESS_TASK_PROMPT_FILE"] = str(args.prompt_file)
    if args.workspace_installer:
        os.environ["HARNESS_WORKSPACE_INSTALLER"] = args.workspace_installer
    else:
        os.environ.pop("HARNESS_WORKSPACE_INSTALLER", None)
    results_dir = args.results_dir.expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    python_paths = [str(GT_ROOT), str(GT_ROOT / "external" / "cybergym" / "src")]
    if os.environ.get("PYTHONPATH"):
        python_paths.append(os.environ["PYTHONPATH"])
    os.environ["PYTHONPATH"] = os.pathsep.join(python_paths)
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
    ensure_arvo_source(args.arvo_id)

    # The remote PoC evaluation protocol requires the lifecycle-only artifact
    # overlay. It wraps the pinned checkout at process entry without editing
    # external/OpenHands.
    analysis_output = results_dir / sample_id / "analysis.json"
    sample_output_dir = results_dir / sample_id
    analysis_output = sample_output_dir / "analysis.json"
    sample_output_dir.mkdir(parents=True, exist_ok=True)
    os.environ["OPENHANDS_HARNESS_MODE"] = "evaluation"
    os.environ["OPENHANDS_CAPTURE_FINE_TRACE"] = "1"
    os.environ.pop("OPENHANDS_ANALYSIS_ARTIFACT_OUTPUT", None)
    os.environ["OPENHANDS_ANALYSIS_OUTPUT"] = str(analysis_output)
    os.environ.pop("OPENHANDS_FINE_TRACE_OUTPUT", None)
    os.environ.pop("OPENHANDS_VULN_LOGIC_OUTPUT", None)
    os.environ["OPENHANDS_EXPECTED_SAMPLE_ID"] = sample_id
    # Do not inherit an entrypoint from a parent experiment.
    os.environ["OPENHANDS_MAIN_MODULE"] = "harness_runtime.openhands.overlay"

    last_status = None
    for attempt in range(1, args.max_attempts + 1):
        clear_previous_result(sample_output_dir)
        os.environ["OPENHANDS_PRE_FINALIZATION_CHECKPOINT"] = str(
            sample_output_dir / "checkpoint" / "pre_finalization"
        )
        # A fresh episode each attempt: overwrite this sample's analysis artifact
        # only when it reaches a normal endpoint. Start clean so stale output from
        # a prior early-died attempt cannot be mistaken for this attempt's output.
        unlink_if_exists(analysis_output)
        unlink_if_exists(analysis_output.with_name("analysis_artifact.json"))
        unlink_if_exists(analysis_output.with_name("analysis_artifact.response.txt"))
        print(f"[*] {sample_id}: generation attempt {attempt}/{args.max_attempts}")
        last_status = run_attempt(
            args, task_id, task_id_safe, sample_id, results_dir
        )
        if last_status in ("success", "iteration_cap", "agent_finished") and analysis_output.exists():
            print(f"[*] {sample_id}: clean endpoint on attempt {attempt} (status={last_status}); analysis artifact captured")
            return
        print(f"[*] {sample_id}: attempt {attempt} did not yield an analysis artifact "
              f"(status={last_status}); {'retrying' if attempt < args.max_attempts else 'giving up'}")
    print(f"[!] {sample_id}: no analysis artifact after {args.max_attempts} attempts (last status={last_status})")
    sys.exit(1)


if __name__ == "__main__":
    main()
