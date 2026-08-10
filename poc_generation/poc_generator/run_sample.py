#!/usr/bin/env python3
"""Drive one CyberGym+OpenHands PoC-generation run for a single GT sample.

Runs the actual generation attempt (tools + real sandbox repo) in a throwaway
scratch dir, then copies only the durable checkpoint pieces (file_store,
trajectory, config.toml, args.json -- NOT the extracted repo/workspace, which
can be 1-2GB and is not part of the durable session state) into
poc_generation/<sample_id>/checkpoint/, and writes poc_generation/<sample_id>/manifest.json.
The subject's GT-shaped final fine trace is captured as part of the same task.
"""
import argparse
import atexit
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

ROOT = Path(__file__).resolve().parent            # poc_generation/poc_generator/
GT_ROOT = ROOT.parents[1]                          # repo root (external/, config.txt, poc_results/)
DEFAULT_POC_RESULTS = ROOT.parent / "poc_results"  # overridden per model by --results-dir
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(GT_ROOT))
sys.path.insert(0, str(GT_ROOT / "external" / "cybergym" / "src"))

from run_openhands_cybergym import (  # noqa: E402
    configure_harness_profile,
    run_with_configs,
    OpenhandsArgs,
    LLMArgs,
    TaskArgs,
)
from cybergym.task.types import TaskDifficulty  # noqa: E402
from check_success import check as check_success  # noqa: E402
from poc_dedup import deduplicate_submission_attempts  # noqa: E402
from reward_framework.harness_repository import HarnessRepository  # noqa: E402


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
    try:
        docker.from_env().containers.run(
            "alpine:3.23",
            command=["chown", "-R", f"{os.getuid()}:{os.getgid()}", "/scratch"],
            volumes={str(scratch): {"bind": "/scratch", "mode": "rw"}},
            remove=True,
        )
        shutil.rmtree(scratch)
    except Exception as exc:
        logging.warning("Could not fully clean scratch %s: %s", scratch, exc)


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


def _ensure_arvo_source_locked(arvo_id: str, arvo_dir: Path) -> Path:
    description_path = arvo_dir / "description.txt"
    if not description_path.is_file():
        selected_path = GT_ROOT / "dataset" / "selected_1000.json"
        selected = json.loads(selected_path.read_text(encoding="utf-8"))
        sample_id = f"arvo_{arvo_id}"
        description = next(
            (
                str(item.get("issue_description") or "").strip()
                for item in selected
                if isinstance(item, dict) and item.get("sample_id") == sample_id
            ),
            "",
        )
        if not description:
            raise RuntimeError(
                f"{sample_id} has no issue_description in {selected_path}"
            )
        description_path.write_text(description + "\n", encoding="utf-8")

    repo_dir = arvo_dir / "repo-vul"
    source_dir = repo_dir / "src-vul"
    image = f"n132/arvo:{arvo_id}-vul"
    client = docker.from_env()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        logging.info("Pulling missing target image %s", image)
        client.images.pull(image)
    if source_dir.is_dir() and any(source_dir.iterdir()):
        return repo_dir

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
    normalized = model[len("openai/"):] if model.startswith("openai/") else model
    if normalized.startswith("gpt-5.4"):
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
    sample_dir: Path, cybergym_agent_id: str, attempts: list[dict]
) -> list[dict]:
    """Copy the immutable server ledger into this model/sample result tree."""
    source_root = ROOT / "server" / "logs" / "submissions" / cybergym_agent_id
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
        record = dict(attempt)
        record["result_path"] = (
            f"submissions/{attempt_id}/" if destination.is_dir() else None
        )
        record["sequence_in_run"] = sequence
        persisted.append(record)
    return persisted


def persist_reward_framework_state(
    run_dir: Path, sample_dir: Path
) -> tuple[dict, list[dict]] | None:
    """Persist and normalize the native Reward Framework submission ledger.

    Native ``submit_candidate`` calls intentionally bypass the legacy CyberGym
    HTTP submission database.  Treat the framework's crash-safe state directory
    as the authoritative ledger whenever it exists, and expose its attempts in
    the same manifest-level view used by ordinary evaluation runs.
    """
    source = run_dir / "reward_framework"
    if not (source / "task_context.json").is_file():
        return None

    destination = sample_dir / "reward_framework"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    persisted_index = destination / "candidates" / "index.json"
    index = (
        json.loads(persisted_index.read_text(encoding="utf-8"))
        if persisted_index.is_file()
        else {"attempts": []}
    )
    normalized = []
    for sequence, metadata in enumerate(index.get("attempts") or [], 1):
        attempt_number = int(metadata["attempt_number"])
        candidate_id = str(metadata["candidate_id"])
        attempt_rel = Path("reward_framework/candidates") / candidate_id / "attempts" / f"attempt_{attempt_number:04d}"
        evidence_rel = Path("reward_framework/evidence") / f"attempt_{attempt_number:04d}.json"
        evidence_path = sample_dir / evidence_rel
        evidence = (
            json.loads(evidence_path.read_text(encoding="utf-8"))
            if evidence_path.is_file() else {}
        )
        runtime = evidence.get("runtime") or {}
        trace_rel = attempt_rel / "trace.json"
        poc_rel = Path("reward_framework/candidates") / candidate_id / "poc"
        runtime_rel = attempt_rel / "current_runtime.json"
        trace_path = sample_dir / trace_rel
        trace_valid = False
        try:
            trace_value = json.loads(trace_path.read_text(encoding="utf-8"))
            trace_valid = isinstance(trace_value, (list, dict))
        except (OSError, TypeError, json.JSONDecodeError):
            pass
        normalized.append({
            "attempt_id": f"reward_attempt_{attempt_number:04d}",
            "attempt_number": attempt_number,
            "sequence_in_run": sequence,
            "source": "reward_framework",
            "candidate_id": candidate_id,
            "duplicate_of": metadata.get("duplicate_of"),
            "poc_hash": metadata.get("sha256"),
            "trace_valid": trace_valid,
            "vul_exit_code": runtime.get("exit_code"),
            "crashed": runtime.get("trigger_observed") is True,
            "trigger_observed": runtime.get("trigger_observed") is True,
            "assessment": evidence.get("assessment"),
            "feedback": evidence.get("feedback"),
            "result_path": f"{attempt_rel.as_posix()}/",
            "trace_path": trace_rel.as_posix(),
            "poc_path": poc_rel.as_posix(),
            "runtime_output_path": (
                runtime_rel.as_posix()
                if (sample_dir / runtime_rel).is_file() else evidence_rel.as_posix()
            ),
            "evidence_path": evidence_rel.as_posix() if evidence_path.is_file() else None,
        })

    observation_path = destination / "observation_state.json"
    observation = (
        json.loads(observation_path.read_text(encoding="utf-8"))
        if observation_path.is_file() else {}
    )
    crashed = [attempt for attempt in normalized if attempt["trigger_observed"]]
    result = {
        "ok": True,
        "source": "reward_framework",
        "submissions": normalized,
        "num_submissions": len(normalized),
        "submission_attempts": normalized,
        "num_submission_attempts": len(normalized),
        "num_crashed": len(crashed),
        "success": bool(crashed),
        "terminal_reason": observation.get("terminal_reason"),
        "state_path": "reward_framework/observation_state.json",
    }
    return result, normalized


def clear_previous_result(sample_dir: Path) -> None:
    """Remove the previous result for this exact model/sample before rerunning.

    Model namespaces already isolate DeepSeek from GPT, so retaining another
    per-run archive only duplicates large checkpoints. A rerun is an explicit
    replacement of the prior result.
    """
    for name in ("checkpoint", "submissions", "runs", "reward_framework"):
        path = sample_dir / name
        if path.is_dir():
            shutil.rmtree(path)
    for name in (
        "manifest.json",
        "fine_trace.json",
        "fine_trace.response.txt",
    ):
        (sample_dir / name).unlink(missing_ok=True)


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
            update_harness=not getattr(args, "freeze_harness_updates", False),
        )
        # configure_harness_profile("baseline") intentionally selects the
        # pristine upstream entrypoint.  This evaluator still needs the
        # lifecycle-only fine-trace overlay around that pristine controller so
        # iteration/error endpoints freeze the checkpoint and get a bounded,
        # tool-free finalization turn.
        if (
            harness_profile == "baseline"
            and os.getenv("OPENHANDS_CAPTURE_FINE_TRACE") == "1"
        ):
            os.environ["OPENHANDS_MAIN_MODULE"] = (
                "poc_generation.openhands_fine_trace_main"
            )
        if harness_profile == "reward":
            version = os.getenv("REWARD_FRAMEWORK_EPISODE_HARNESS_VERSION", "1")
            os.environ["REWARD_FRAMEWORK_BASELINE_PROFILE"] = (
                f"openhands_evolved_v{version}"
            )
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
        repo=Path(
            os.getenv("REWARD_FRAMEWORK_EPISODE_OPENHANDS_ROOT", args.openhands_repo)
        ).expanduser().resolve(),
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
            logging.warning(f"run_with_configs raised {exc!r}; still attempting checkpoint save from partial state")
            returned_agent_id = None

        run_dir = find_run_dir(openhands_args.log_dir, task_id_safe, returned_agent_id)
        if run_dir is None:
            print(json.dumps({"arvo_id": args.arvo_id, "status": "no_run_dir_found"}, indent=2))
            return None

        args_json = json.loads((run_dir / "args.json").read_text())
        cybergym_agent_id = args_json["task"]["agent_id"]

        sample_dir = results_dir / sample_id
        reward_result = persist_reward_framework_state(run_dir, sample_dir)
        if reward_result is not None:
            success_info, persisted_attempts = reward_result
        else:
            db_path = ROOT / "server" / "poc.db"
            success_info = (
                check_success(db_path, cybergym_agent_id)
                if db_path.exists() else {"ok": False, "error": "db not found"}
            )
            persisted_attempts = persist_submission_attempts(
                sample_dir,
                cybergym_agent_id,
                success_info.get("submission_attempts") or [],
            )
        poc_deduplication, deduplicated_pocs = (
            deduplicate_submission_attempts(persisted_attempts)
        )
        trace_source = "task_finalization"
        valid_attempts = [
            attempt for attempt in persisted_attempts if attempt.get("trace_valid")
        ]
        if valid_attempts:
            latest = valid_attempts[-1]
            candidate_path = (
                sample_dir / str(latest["trace_path"])
                if latest.get("trace_path")
                else sample_dir / str(latest["result_path"]) / "candidate_trace.json"
            )
            if candidate_path.is_file():
                trace_output = sample_dir / "fine_trace.json"
                shutil.copy2(candidate_path, trace_output)
                shutil.copy2(
                    candidate_path,
                    trace_output.with_name("fine_trace.response.txt"),
                )
                trace_source = (
                    "reward_framework_last_valid_submission"
                    if reward_result is not None else "last_valid_poc_submission"
                )

        # A final fine trace is written ONLY when the episode reaches a clean
        # endpoint (iteration limit / agent finished). Its
        # presence is therefore the reliable signal that the run terminated
        # cleanly -- unlike a trajectory-length heuristic, which a stuck loop
        # inflates past max_iter and so misreports an early death as a genuine
        # iteration cap. No trace + no success => the episode died early
        # (stuck loop / error) and should be re-run (see main).
        trace_produced = (results_dir / sample_id / "fine_trace.json").exists()
        trace_response = results_dir / sample_id / "fine_trace.response.txt"
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
            and "[Fine Trace Finalization]" in trajectory_path.read_text(
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
        elif trace_produced and reached_iteration_cap:
            status = "iteration_cap"
        elif trace_produced and terminal_finish_observed:
            status = "agent_finished"
        elif trace_response.is_file() and reached_iteration_cap:
            status = "iteration_cap_invalid_trace"
        elif trace_response.is_file() and terminal_finish_observed:
            status = "agent_finished_invalid_trace"
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
            "evaluation_protocol": "poc_trace_per_submission_v2",
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
            "agent_action_count": agent_action_count,
            "blocked_premature_finish_count": blocked_finish_count,
            "effective_controller_iterations": effective_controller_iterations,
            "terminal_finish_observed": terminal_finish_observed,
            "status": status,
            "poc_generation": success_info,
            "submission_attempts": persisted_attempts,
            "poc_deduplication": poc_deduplication,
            "deduplicated_pocs": deduplicated_pocs,
            "fine_trace": {
                "path": "fine_trace.json",
                "produced": trace_produced,
                "raw_response_path": (
                    "fine_trace.response.txt" if trace_response.is_file() else None
                ),
                "format": "GT fine_trace JSON array",
                "source": trace_source,
            },
            "checkpoint": {
                "dir": "checkpoint/",
                "phase": (
                    "pre_fine_trace_finalization"
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
            "reward_framework": (
                {
                    "enabled": True,
                    "dir": "reward_framework/",
                    "state_path": "reward_framework/observation_state.json",
                    "trajectory_state_path": "reward_framework/trajectory_state.json",
                    "evidence_state_path": "reward_framework/evidence_state.json",
                    "harness_state_path": "reward_framework/harness_state.json",
                    "task_context_path": "reward_framework/task_context.json",
                    "candidate_index_path": "reward_framework/candidates/index.json",
                    "evidence_dir": "reward_framework/evidence/",
                    "episode_experience_path": (
                        "reward_framework/episode_experience.json"
                        if (sample_dir / "reward_framework/episode_experience.json").is_file()
                        else None
                    ),
                    "cross_sample_update_path": (
                        "reward_framework/cross_sample_update.json"
                        if (sample_dir / "reward_framework/cross_sample_update.json").is_file()
                        else None
                    ),
                    "terminal_reason": success_info.get("terminal_reason"),
                }
                if reward_result is not None else {"enabled": False}
            ),
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
        choices=("baseline", "reward"),
        default="baseline",
        help=(
            "baseline uses pristine OpenHands; reward enables the complete "
            "Reward Framework and cross-sample harness optimization."
        ),
    )
    ap.add_argument(
        "--openhands-repo",
        type=Path,
        default=GT_ROOT / "external" / "OpenHands",
        help="Complete OpenHands checkout containing pyproject.toml.",
    )
    ap.add_argument(
        "--harness-training-dir",
        type=Path,
        default=None,
        help=(
            "Shared GT-free Experience Pool and versioned OpenHands fork for "
            "the reward profile; defaults beside results-dir."
        ),
    )
    ap.add_argument(
        "--freeze-harness-updates",
        action="store_true",
        help=(
            "Use the current learned reward harness without updating its "
            "Experience Pool or source; intended for validation/test."
        ),
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
        default=DEFAULT_POC_RESULTS,
        help=(
            "Directory receiving per-sample results. Batch launchers should use "
            "a model-specific directory so different models cannot overwrite."
        ),
    )
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="Re-run the whole episode up to this many times if it dies early "
                         "(stuck loop / no normal endpoint), since every completed task "
                         "must yield a final fine trace.")
    args = ap.parse_args()
    results_dir = args.results_dir.expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    adaptive_python_root = None
    harness_training_lock = None
    if args.harness_profile == "reward":
        training_root = (
            args.harness_training_dir.expanduser().resolve()
            if args.harness_training_dir is not None
            else results_dir.parent / "harness_training"
        )
        training_root.mkdir(parents=True, exist_ok=True)
        if not args.freeze_harness_updates:
            harness_training_lock = (training_root / ".sample_update.lock").open("a+")
            fcntl.flock(harness_training_lock.fileno(), fcntl.LOCK_EX)
            atexit.register(harness_training_lock.close)
        repository = HarnessRepository(
            training_root / "harness", args.openhands_repo.expanduser().resolve()
        )
        version = repository.initialize()
        # Freeze one source snapshot for every retry of this sample. The global
        # worktree may advance after an episode, but never underneath a sample.
        adaptive_python_root = training_root / "launches" / f"{args.arvo_id}_{os.getpid()}"
        if adaptive_python_root.exists():
            shutil.rmtree(adaptive_python_root)
        adaptive_python_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            repository.worktree, adaptive_python_root,
            ignore=shutil.ignore_patterns(".harness_optimizer", "__pycache__", "*.pyc"),
        )
        atexit.register(shutil.rmtree, adaptive_python_root, True)
        if not args.freeze_harness_updates:
            os.environ["REWARD_FRAMEWORK_TRAINING_ROOT"] = str(training_root)
        os.environ["REWARD_FRAMEWORK_PRISTINE_OPENHANDS"] = str(
            args.openhands_repo.expanduser().resolve()
        )
        os.environ["REWARD_FRAMEWORK_EPISODE_HARNESS_VERSION"] = str(version)
        os.environ["REWARD_FRAMEWORK_EPISODE_OPENHANDS_ROOT"] = str(
            adaptive_python_root
        )

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    python_paths = []
    if adaptive_python_root is not None:
        python_paths.append(str(adaptive_python_root))
    python_paths.extend([str(GT_ROOT), str(GT_ROOT / "external" / "cybergym" / "src")])
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

    # The initial task prompt declares the GT-shaped fine trace as a required
    # final deliverable. The harness only supplies a bounded format-only final
    # turn at the iteration limit; it does not start a probe or reveal GT.
    trace_output = results_dir / sample_id / "fine_trace.json"
    trace_output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["OPENHANDS_HARNESS_MODE"] = "evaluation"
    os.environ["OPENHANDS_CAPTURE_FINE_TRACE"] = "1"
    os.environ["OPENHANDS_FINE_TRACE_OUTPUT"] = str(trace_output)
    # Do not inherit the upstream entrypoint from a parent experiment: the
    # evaluation protocol requires the checkpoint/fine-trace overlay.
    os.environ["OPENHANDS_MAIN_MODULE"] = "poc_generation.openhands_fine_trace_main"

    last_status = None
    for attempt in range(1, args.max_attempts + 1):
        clear_previous_result(trace_output.parent)
        os.environ["OPENHANDS_PRE_FINALIZATION_CHECKPOINT"] = str(
            trace_output.parent / "checkpoint" / "pre_finalization"
        )
        # A fresh episode each attempt: overwrite this sample's fine_trace.json
        # only when it reaches a normal endpoint. Start clean so a stale trace from a
        # prior early-died attempt cannot be mistaken for this attempt's output.
        trace_output.unlink(missing_ok=True)
        trace_output.with_name("fine_trace.response.txt").unlink(missing_ok=True)
        print(f"[*] {sample_id}: generation attempt {attempt}/{args.max_attempts}")
        last_status = run_attempt(
            args, task_id, task_id_safe, sample_id, results_dir
        )
        if last_status in ("success", "iteration_cap", "agent_finished") and trace_output.exists():
            print(f"[*] {sample_id}: clean endpoint on attempt {attempt} (status={last_status}); fine trace captured")
            return
        print(f"[*] {sample_id}: attempt {attempt} did not yield a trace "
              f"(status={last_status}); {'retrying' if attempt < args.max_attempts else 'giving up'}")
    print(f"[!] {sample_id}: no fine trace after {args.max_attempts} attempts (last status={last_status})")
    sys.exit(1)


if __name__ == "__main__":
    main()
