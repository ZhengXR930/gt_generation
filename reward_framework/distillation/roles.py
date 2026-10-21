"""Distillation roles as agent subsessions.

The diagnostician, Teacher, and Curator are agents, not one-shot completions.
Each gets a working directory of input files and writes one JSON artifact back,
exactly the way the coding agent writes `analysis.json`. That buys three things
a single chat call cannot: the diagnostician reads the *whole* trajectory
instead of a truncated excerpt, every role can grep and diff its inputs with
real tools, and all four agents in the pipeline run on the same harness and
model, so provenance stays comparable.

Role workspaces deliberately get isolated agent state: the PoC reproduction and
submission skills belong to the agent under study, not to the roles studying it.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_runtime.auth import load_env_key
from harness_runtime.subsession import SubsessionSpec, run_codex_subsession

from .artifacts import GT_RESULTS, REPO_ROOT, load_json_if_exists, write_json
from .outcome import (
    OBSERVED_STAGE_ORDER,
    classify_outcome,
    diagnosis_case_profile,
    diagnostics_summary,
    ordered_candidate_sequence,
)
from .prompts import role_task_prompt
from .skill_packet import lessons_snapshot

OUTPUT_NAME = "OUTPUT.json"
STATUS_NAME = "STATUS.json"

# A subsession that exited non-zero or ran out of time failed on its own terms:
# there is nothing to salvage, so it starts over. One that ran to completion but
# left no usable artifact only has to finish the write, and its workspace still
# holds whatever it worked out, so it is re-entered instead.
RESUMABLE = ("missing_output", "invalid_output")

# The diagnostician explains an evaluator-owned case profile from the agent's
# own evidence.  It deliberately does not receive GT/reference artifacts:
# copying them tempted it to restate a reference witness as if it explained the
# agent's behavior.  The public issue description is the only sample-side
# annotation the evaluated agent could itself have read.
PUBLIC_FILES = (("description.txt", "public vulnerability description"),)

TRAJECTORY_CANDIDATES = (
    "checkpoint/trajectory",
    "checkpoint/trajectory.json",
    "checkpoint/agent.log",
    "checkpoint/openhands_stdout.txt",
    "checkpoint/codex_stdout.txt",
    "checkpoint/claude_stdout.txt",
)

# The raw agent session is the primary behavioral evidence for a diagnosis.
# `checkpoint/trajectory` is sometimes only a small runner index pointing at
# the real session, so it must never satisfy this contract by itself.
DSH_SESSION_PATTERNS = (
    "dsh_home/sessions-jsonl/**/session*.jsonl",
    "dsh_home/sessions/**/session*.jsonl",
)
CLAUDE_SESSION_PATTERNS = (
    "file/sessions/**/events/*.json",
    "file/sessions/**/event_cache/*.json",
)
RAW_TRAJECTORY_PATTERNS = (
    *DSH_SESSION_PATTERNS,
    *CLAUDE_SESSION_PATTERNS,
    "**/*stdout*.jsonl",
    "**/*transcript*.jsonl",
    "**/*conversation*.jsonl",
    "**/*claude*.jsonl",
    "**/agent.log",
    "**/openhands_stdout.txt",
    "**/codex_stdout.txt",
    "**/claude_stdout.txt",
)


def _unique_files(paths: list[Path]) -> list[Path]:
    """Return existing files once, in a deterministic order."""
    return sorted({path.resolve() for path in paths if path.is_file()})


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl_events(paths: list[Path]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in paths:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
        except OSError:
            continue
    return events


def _event_text(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True)


def _short_evidence(items: list[str], limit: int = 3) -> list[str]:
    return [item[:1200] for item in items[:limit]]


def _tool_call_signature(event: dict[str, Any]) -> tuple[str, str] | None:
    if event.get("type") != "tool/call":
        return None
    message = event.get("data") or {}
    if isinstance(message.get("message"), dict):
        message = message["message"]
    name = str(message.get("name") or "")
    arguments = message.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    return name, json.dumps(arguments, ensure_ascii=False, sort_keys=True)[:1000]


def _tool_read_path(event: dict[str, Any]) -> str | None:
    if event.get("type") != "tool/call":
        return None
    message = event.get("data") or {}
    if isinstance(message.get("message"), dict):
        message = message["message"]
    name = str(message.get("name") or "")
    arguments = message.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"command": arguments}
    if not isinstance(arguments, dict):
        return None
    if name == "read":
        value = arguments.get("file_path") or arguments.get("path")
        return str(value) if value else None
    if name in {"bash", "shell", "exec"}:
        command = str(arguments.get("command") or "")
        paths = re.findall(r"(?:/workspace|/home/[A-Za-z0-9_.-]+|repo-vul)[^\s;|'\"]*", command)
        return paths[0] if paths else None
    return None


def _is_submit_call(event: dict[str, Any]) -> bool:
    if event.get("type") != "tool/call":
        return False
    message = event.get("data") or {}
    if isinstance(message.get("message"), dict):
        message = message["message"]
    arguments = message.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"command": arguments}
    command = str((arguments or {}).get("command") or "") if isinstance(arguments, dict) else ""
    return bool(re.search(r"(?:^|[;&]\s*)(?:bash\s+)?(?:\./)?submit\.sh\b", command))


def _trajectory_harness_checklist(
    sample_dir: Path,
    raw_session_files: list[Path],
    trajectory_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Build bounded, evaluator-owned process checks for the diagnostician."""
    events = _jsonl_events(raw_session_files)
    event_texts = [_event_text(event) for event in events]
    tool_calls = [event for event in events if event.get("type") == "tool/call"]
    tool_results = [event for event in events if event.get("type") == "tool/result"]
    signatures = [_tool_call_signature(event) for event in tool_calls]
    signatures = [item for item in signatures if item is not None]
    read_paths = [path for path in (_tool_read_path(event) for event in tool_calls) if path]
    repeated_read_paths = sorted({path for path in read_paths if read_paths.count(path) > 1})
    repeated_commands = sum(
        max(0, signatures.count(signature) - 1)
        for signature in sorted(set(signatures))
    )
    submit_events = [
        event for event in tool_calls if _is_submit_call(event)
    ]
    submit_steps = [
        (event.get("data") or {}).get("step")
        for event in submit_events
    ]
    submit_steps = [int(step) for step in submit_steps if isinstance(step, int)]
    gaps = [b - a for a, b in zip(submit_steps, submit_steps[1:])]
    compression_hits = [
        text for event, text in zip(events, event_texts)
        if event.get("type") not in {"request/context", "request/header", "user/message"}
        and re.search(
            r"compaction|context.{0,30}(?:compressed|summarized|trimmed|pruned)|conversation.{0,30}summarized",
            text,
            re.I,
        )
    ]
    truncation_hits = [
        text for event, text in zip(events, event_texts)
        if event.get("type") in {"tool/result", "assistant/message", "system/message"}
        and re.search(r"output.{0,20}truncat|truncat.{0,20}output|response.{0,20}truncat", text, re.I)
    ]
    tool_error_hits = [
        text for text in event_texts
        if re.search(r'"isError"\s*:\s*true|tool.{0,20}(error|failed)|permission denied|command not found', text, re.I)
    ]
    environment_hits = [
        text for text in event_texts
        if re.search(r"compile failed|compilation error|runner error|container error|permission denied|timed out|timeout|dependency.{0,20}missing|command not found", text, re.I)
    ]
    trajectory = {}
    trajectory_path = sample_dir / "checkpoint" / "trajectory"
    try:
        trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    manifest = load_json_if_exists(sample_dir / "manifest.json") or {}
    return {
        "protocol": "diagnosis-harness-checklist-v1",
        "context_management": {
            "raw_session_files": len(raw_session_files),
            "compression_or_compaction_events": len(compression_hits),
            "context_visit_available": (sample_dir / "context_visit.json").is_file(),
            "possible_context_loss": "unknown",
            "repeated_read_paths": repeated_read_paths[:50],
            "reads_after_compression": "unknown",
            "key_facts_lost": "unknown",
            "evidence": _short_evidence(compression_hits),
        },
        "spinning": {
            "tool_calls": len(tool_calls),
            "repeated_identical_tool_calls": repeated_commands,
            "submission_calls_seen": len(submit_events),
            "consecutive_no_progress_steps": "unknown",
            "evidence": "candidate sequence and raw session must be compared by diagnostician",
        },
        "output_truncation": {
            "signals": len(truncation_hits),
            "evidence": _short_evidence(truncation_hits),
        },
        "tool_calls": {
            "calls": len(tool_calls),
            "results": len(tool_results),
            "error_signals": len(tool_error_hits),
            "consecutive_failures": "unknown",
            "evidence": _short_evidence(tool_error_hits),
        },
        "environment": {
            "error_signals": len(environment_hits),
            "evidence": _short_evidence(environment_hits),
        },
        "submission_timing": {
            "first_submission_step": submit_steps[0] if submit_steps else None,
            "submission_steps": submit_steps,
            "gaps_between_submissions": gaps,
            "no_submission_observed": not bool(submit_steps),
        },
        "ending": {
            "stop_reason": trajectory.get("stop_reason"),
            "completed_steps": trajectory.get("completed_steps"),
            "returncode": trajectory.get("returncode"),
            "manifest_status": manifest.get("status"),
            "agent_terminal_status": manifest.get("agent_terminal_status"),
        },
    }


def _candidate_artifact_path(
    sample_dir: Path,
    candidate: dict[str, Any],
    field: str,
    fallback_name: str,
) -> Path | None:
    """Resolve an evaluator candidate artifact to the persisted submission.

    Runtime evaluation records identify submissions by ``attempt_id`` and
    usually omit the artifact paths that the diagnostician needs.  Prefer an
    explicit path when one is present, but fall back to the harness-owned
    ``submissions/<attempt_id>/<file>`` layout.
    """
    raw_path = candidate.get(field)
    paths: list[Path] = []
    if raw_path:
        path = Path(str(raw_path))
        paths.append(path if path.is_absolute() else sample_dir / path)
    attempt_id = str(candidate.get("attempt_id") or "")
    if attempt_id:
        paths.append(sample_dir / "submissions" / attempt_id / fallback_name)
    for path in paths:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _candidate_artifact_relpath(
    sample_dir: Path,
    candidate: dict[str, Any],
    field: str,
    fallback_name: str,
) -> str | None:
    path = _candidate_artifact_path(sample_dir, candidate, field, fallback_name)
    if path is None:
        return None
    try:
        return str(path.relative_to(sample_dir))
    except ValueError:
        # Keep an explicitly supplied artifact outside the result tree
        # inspectable rather than silently dropping it from the evidence.
        return str(path)


def _candidate_bytes(sample_dir: Path, candidate: dict[str, Any]) -> bytes | None:
    path = _candidate_artifact_path(sample_dir, candidate, "poc_path", "poc.bin")
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _manifest_submission_attempts(sample_dir: Path) -> list[dict[str, Any]]:
    manifest = load_json_if_exists(sample_dir / "manifest.json") or {}
    if not isinstance(manifest, dict):
        return []
    generation = manifest.get("poc_generation")
    if isinstance(generation, dict) and isinstance(generation.get("submission_attempts"), list):
        return generation["submission_attempts"]
    return manifest.get("submission_attempts") if isinstance(manifest.get("submission_attempts"), list) else []


def _candidate_change(previous: bytes | None, current: bytes | None) -> dict[str, Any]:
    if current is None:
        return {"kind": "candidate_bytes_unavailable"}
    if previous is None:
        return {"kind": "first_candidate", "length": len(current)}
    if previous == current:
        return {"kind": "duplicate_bytes", "length": len(current)}
    first = next((i for i, (left, right) in enumerate(zip(previous, current)) if left != right), None)
    return {
        "kind": "length_or_structure_change" if len(previous) != len(current) else "byte_value_change",
        "previous_length": len(previous),
        "length": len(current),
        "length_delta": len(current) - len(previous),
        "changed_byte_count": sum(left != right for left, right in zip(previous, current)) + abs(len(previous) - len(current)),
        "first_changed_offset": first,
    }


def _candidate_sequence_view(sample_dir: Path, eval_row: dict[str, Any] | None) -> list[dict[str, Any]]:
    submission_attempts = _manifest_submission_attempts(sample_dir)
    sequence = ordered_candidate_sequence(eval_row, submission_attempts=submission_attempts)
    previous: bytes | None = None
    out: list[dict[str, Any]] = []
    runtime_candidates = {
        str(item.get("attempt_id")): item
        for item in ((eval_row or {}).get("runtime") or {}).get("candidates") or []
        if isinstance(item, dict)
    }
    for item in sequence:
        # Occurrence records can represent a duplicate that the evaluator
        # omitted from runtime.candidates.  In that case the sequence item is
        # still sufficient to resolve its own persisted artifact directory.
        candidate = runtime_candidates.get(str(item.get("attempt_id")), item)
        current = _candidate_bytes(sample_dir, candidate)
        change = _candidate_change(previous, current)
        row = dict(item)
        row["poc_path"] = _candidate_artifact_relpath(
            sample_dir, candidate, "poc_path", "poc.bin"
        )
        row["analysis_path"] = _candidate_artifact_relpath(
            sample_dir, candidate, "analysis_path", "analysis.json"
        )
        row["runtime_output_path"] = _candidate_artifact_relpath(
            sample_dir, candidate, "runtime_output_path", "runtime_output.txt"
        )
        row["candidate_size"] = len(current) if current is not None else None
        row["change_from_previous"] = change
        row["runtime_output_available"] = row["runtime_output_path"] is not None
        row["analysis_available"] = row["analysis_path"] is not None
        out.append(row)
        previous = current
    return out


def _vulnerability_checklist(
    eval_row: dict[str, Any] | None,
    candidate_sequence: list[dict[str, Any]],
) -> dict[str, Any]:
    runtime = (eval_row or {}).get("runtime") or {}
    target_hits = sum(item.get("target_vulnerability_triggered") is True for item in candidate_sequence)
    crashes = sum(bool(item.get("crash_observed")) for item in candidate_sequence)
    conflicts = [
        item.get("sequence") for item in candidate_sequence
        if item.get("target_vulnerability_triggered") is True
        and item.get("deepest_stage") not in {"target_issue", "sensitive_operation"}
    ]
    deepest = "none"
    for item in candidate_sequence:
        if item.get("stage_delta_from_previous") == "advanced" or deepest == "none":
            # Overall progress is the observed axis.  ``deepest_stage`` is
            # still the target-ladder view and may intentionally remain R2
            # for a non-target crash.
            deepest = str(item.get("observed_stage") or item.get("deepest_stage") or "none")
    target_deepest = max(
        (str(item.get("deepest_stage") or "none") for item in candidate_sequence),
        key=lambda stage: OBSERVED_STAGE_ORDER.index(stage) if stage in OBSERVED_STAGE_ORDER else 0,
        default="none",
    )
    return {
        "protocol": "diagnosis-vulnerability-checklist-v2",
        "candidate_count": len(candidate_sequence),
        "deepest_observed_stage_overall": deepest,
        "deepest_target_stage_overall": target_deepest,
        "deepest_stage_overall": deepest,
        "target_match_count": target_hits,
        "crash_count": crashes,
        "trace_conflict_sequences": conflicts,
        "candidate_sequence": candidate_sequence,
        "offline_replay_is_not_agent_feedback": True,
        "runtime_summary": {
            "submitted_unique_pocs": runtime.get("submitted_unique_pocs"),
            "submitted_occurrences": len(candidate_sequence),
            "executed_candidates": runtime.get("reachability_executed_candidates"),
            "unavailable": runtime.get("unavailable"),
        },
    }


def _diagnosis_outcome_rank(record: dict[str, Any] | None) -> tuple[int, int]:
    value = record or {}
    outcome = str(value.get("outcome") or "").lower()
    if outcome == "success" or value.get("triggered") is True:
        return (3, int(value.get("submitted_unique_pocs") or 0))
    if int(value.get("submitted_unique_pocs") or 0) > 0:
        return (2, int(value.get("submitted_unique_pocs") or 0))
    if outcome in {"failure", "partial"}:
        return (1, 0)
    return (0, 0)


def diagnosis_trajectory_evidence(sample_dir: Path) -> dict[str, Any]:
    """Inventory raw trajectory evidence and reject known harness runs without it.

    This is shared by the batch preflight and workspace builder.  Keeping one
    contract prevents a diagnosis from starting with a metadata pointer in
    place of the coding agent's actual session.
    """
    manifest = load_json_if_exists(sample_dir / "manifest.json") or {}
    harness = str(manifest.get("harness") or "").strip().lower()
    checkpoint = (sample_dir / "checkpoint").resolve()
    dsh_checkpoint = (checkpoint / "dsh_home").is_dir()
    claude_checkpoint = (checkpoint / "file" / "sessions").is_dir()
    if harness in {"deepseek_harness", "dsh"} or dsh_checkpoint:
        patterns = DSH_SESSION_PATTERNS
    elif claude_checkpoint:
        patterns = CLAUDE_SESSION_PATTERNS
    else:
        patterns = RAW_TRAJECTORY_PATTERNS
    files = _unique_files(
        [path for pattern in patterns for path in checkpoint.glob(pattern)]
    )
    # A result with a declared harness or DSH checkpoint is an agent run, not
    # a legacy import.  A raw session/transcript is mandatory for diagnosis.
    required = (sample_dir / "manifest.json").is_file() or dsh_checkpoint
    if required and not files:
        raise ValueError(
            f"diagnosis requires raw agent session evidence for {sample_dir.name} "
            f"(harness={harness or 'unknown'}); checkpoint metadata alone is insufficient"
        )
    return {
        "protocol": "diagnosis-trajectory-evidence-v1",
        "sample_id": sample_dir.name,
        "harness": harness or None,
        "raw_session_required": required,
        "raw_session_files": [
            {
                "path": str(path.relative_to(checkpoint)),
                "bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
            for path in files
        ],
    }

CHECKPOINT_FILES = (
    "status.json",
    "task.json",
    "workspace_listing.txt",
    "analysis_candidates.json",
    "returncode.txt",
    "args.json",
)

RUN_LOG_PATTERNS = ("*.log", "*.txt", "*/bridge.log")

SUBMISSION_FILES = (
    "result.json",
    "analysis.json",
    "runtime_output.txt",
    "request.json",
    "candidate_trace.json",
    "candidate_trace.response.txt",
    "poc.bin",
)


@dataclass(frozen=True)
class RoleRun:
    role: str
    workspace: Path
    prompt: str
    output_file: Path

    @property
    def inputs(self) -> Path:
        return self.workspace / "inputs"


def plan_role(workspace: Path, output_file: Path) -> tuple[str, str]:
    """Decide how to (re-)run a role from what its workspace already holds.

    Returns one of ``reuse`` (a good artifact is already there), ``resume``
    (re-enter the existing workspace to finish the write), or ``fresh``.
    """
    if isinstance(load_json_if_exists(output_file), dict):
        return "reuse", "artifact already present"
    if not (workspace / "TASK.md").is_file():
        return "fresh", "no previous workspace"
    previous = load_json_if_exists(workspace / STATUS_NAME) or {}
    status = str(previous.get("status") or "")
    if status in RESUMABLE:
        return "resume", f"previous session ended as {status}"
    return "fresh", f"previous session ended as {status or 'unknown'}"


def _source_tree_for_sample(sample_id: str) -> Path | None:
    """Return the same vulnerable source tree shape the coding agent receives."""
    if sample_id.startswith("arvo_"):
        arvo_id = sample_id[len("arvo_"):]
        path = REPO_ROOT / "external" / "cybergym_data_subset" / "data" / "arvo" / arvo_id / "repo-vul" / "src-vul"
    else:
        path = GT_RESULTS / sample_id / "_work" / "src"
    return path if path.is_dir() else None


def resume_role(role: str, workspace: Path, *, reason: str) -> RoleRun:
    """Re-enter a workspace whose session ended without writing its artifact.

    The original task is replayed verbatim from TASK.md so the role sees exactly
    what it saw before, prefixed with what went wrong and an instruction not to
    redo work whose notes are still on disk.
    """
    task = (workspace / "TASK.md").read_text(encoding="utf-8")
    prompt = (
        f"A previous session in this directory ended without a usable `{OUTPUT_NAME}` ({reason}).\n"
        "Its inputs, and any notes or scratch files it left, are still here. Do not start the\n"
        f"analysis over: read what is already in this directory, finish it, and write `{OUTPUT_NAME}`.\n\n"
        "The original task follows.\n\n" + task
    )
    return RoleRun(role, workspace, prompt, workspace / OUTPUT_NAME)


class _Workspace:
    """Builds a role directory and the index of what it contains.

    Every `put`/`copy` both places a file and records the line the role's prompt
    will use to find it, so the directory and its description cannot drift.
    """

    def __init__(self, root: Path) -> None:
        if root.exists():
            shutil.rmtree(root)
        self.root = root
        self.inputs = root / "inputs"
        self.inputs.mkdir(parents=True)
        self.index: list[tuple[str, str]] = []

    def put(self, name: str, payload: Any, description: str) -> None:
        write_json(self.inputs / name, payload)
        self.index.append((f"inputs/{name}", description))

    def copy(self, source: Path, name: str, description: str) -> bool:
        if not source.is_file():
            return False
        target = self.inputs / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        self.index.append((f"inputs/{name}", description))
        return True

    def copy_first(self, sources: list[Path], name: str, description: str) -> bool:
        return any(self.copy(source, name, description) for source in sources)

    def symlink(self, source: Path | None, name: str, description: str) -> bool:
        if source is None or not source.exists():
            return False
        target = self.inputs / name
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(source.resolve(), target_is_directory=source.is_dir())
        self.index.append((f"inputs/{name}/", description))
        return True

    def copy_files(self, source: Path, name: str, description: str, *, keep: tuple[str, ...]) -> None:
        if not source.is_dir():
            return
        copied = False
        for filename in keep:
            item = source / filename
            if item.is_file():
                target = self.inputs / name
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target / filename)
                copied = True
        if copied:
            self.index.append((f"inputs/{name}/", description))

    def copy_tree(self, source: Path, name: str, description: str, *, keep: tuple[str, ...]) -> None:
        if not source.is_dir():
            return
        copied = False
        for child in sorted(p for p in source.iterdir() if p.is_dir()):
            for filename in keep:
                if (child / filename).is_file():
                    target = self.inputs / name / child.name
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(child / filename, target / filename)
                    copied = True
        if copied:
            self.index.append((f"inputs/{name}/", description))

    def copy_glob_tree(self, source: Path, name: str, description: str, *, patterns: tuple[str, ...]) -> None:
        if not source.is_dir():
            return
        copied = False
        for pattern in patterns:
            for item in sorted(source.glob(pattern)):
                if item.is_file():
                    rel = item.relative_to(source)
                    target = self.inputs / name / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
                    copied = True
        if copied:
            self.index.append((f"inputs/{name}/", description))

    def copy_selected_files(self, source: Path, name: str, description: str, *, files: list[Path]) -> None:
        """Copy an explicit, validated set while preserving paths below source."""
        copied = False
        for item in files:
            if not item.is_file():
                continue
            target = self.inputs / name / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied = True
        if copied:
            self.index.append((f"inputs/{name}/", description))

    def copy_packet_as(self, packet: Path, name: str, description: str, *, snapshot_name: str | None = None) -> None:
        shutil.copytree(packet, self.inputs / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.index.append((f"inputs/{name}/", description))
        if snapshot_name:
            self.put(snapshot_name, lessons_snapshot(packet),
                     f"lesson ids and capacity for inputs/{name}/")

    def copy_packet(self, packet: Path) -> None:
        self.copy_packet_as(
            packet,
            "skill_packet",
            "the current packet as the agent receives it; read both SKILL.md files",
            snapshot_name="lessons_snapshot.json",
        )

    def finish(self, role: str, template: str) -> RoleRun:
        prompt = role_task_prompt(template, inputs=self.index, output_rel=OUTPUT_NAME)
        (self.root / "TASK.md").write_text(prompt, encoding="utf-8")
        return RoleRun(role, self.root, prompt, self.root / OUTPUT_NAME)


def build_diagnosis_role(
    workspace: Path,
    sample_id: str,
    results_dir: Path,
    eval_row: dict[str, Any] | None,
    outcome_record: dict[str, Any],
    initial_eval_row: dict[str, Any] | None = None,
) -> RoleRun:
    space = _Workspace(workspace)
    sample_dir = results_dir / sample_id
    checkpoint_dir = sample_dir / "checkpoint"
    trajectory_evidence = diagnosis_trajectory_evidence(sample_dir)
    raw_session_files = [
        checkpoint_dir / record["path"]
        for record in trajectory_evidence["raw_session_files"]
    ]
    submission_attempts = _manifest_submission_attempts(sample_dir)
    # Keep the evaluator row immutable, but make the complete occurrence
    # sequence available to every downstream diagnostic view.  This prevents
    # ``runtime.candidates`` deduplication from changing the stage/sequence
    # semantics seen by the diagnostician and Correction.
    diagnostic_eval_row = eval_row
    if submission_attempts and isinstance(eval_row, dict):
        diagnostic_eval_row = dict(eval_row)
        diagnostic_runtime = dict((eval_row.get("runtime") or {}))
        diagnostic_runtime["submission_attempts"] = submission_attempts
        diagnostic_eval_row["runtime"] = diagnostic_runtime
    candidate_sequence = _candidate_sequence_view(sample_dir, diagnostic_eval_row)
    initial_outcome = classify_outcome(initial_eval_row) if initial_eval_row else None

    for name, description in PUBLIC_FILES:
        space.copy(GT_RESULTS / sample_id / name, name, description)
    space.copy(sample_dir / "analysis.json", "agent_analysis.json",
               "the agent's own analysis artifact for its last candidate")
    space.copy(sample_dir / "manifest.json", "agent_manifest.json",
               "run manifest: submissions, deduplication, stop reason")
    space.copy(sample_dir / "context_visit.json", "context_visit.json",
               "files and functions the coding agent inspected, reconstructed from its checkpoint")
    space.copy_selected_files(
        checkpoint_dir,
        "agent_sessions",
        "complete raw coding-agent session/transcript; primary evidence for what the agent observed, reasoned, and did",
        files=raw_session_files,
    )
    space.put(
        "agent_session_inventory.json",
        trajectory_evidence,
        "verifiable inventory of every raw coding-agent session copied into inputs/agent_sessions/",
    )
    space.put(
        "harness_checklist.json",
        _trajectory_harness_checklist(sample_dir, raw_session_files, trajectory_evidence),
        "evaluator-owned harness/process checklist; explain it using raw sessions, do not recalculate it",
    )
    space.put(
        "vulnerability_checklist.json",
        _vulnerability_checklist(diagnostic_eval_row, candidate_sequence),
        "evaluator-owned reachability checklist and ordered candidate facts; replay records are offline evidence",
    )
    space.put(
        "candidate_sequence.json",
        candidate_sequence,
        "ordered candidates with byte-level change summaries; add trajectory-backed interpretation for each item",
    )
    summary = diagnostics_summary(diagnostic_eval_row)
    space.put("diagnostics_summary.json", summary,
              "compact side-by-side trace, reasoning, and replay diagnostic summaries")
    trace_detail = dict(summary.get("trace_coverage") or {})
    raw_coverage = (diagnostic_eval_row or {}).get("fine_trace_coverage") or {}
    for key in ("node_reports", "edge_reports", "stage_reports"):
        if raw_coverage.get(key) not in (None, "", [], {}):
            trace_detail[key] = raw_coverage[key]
    space.put("diagnostics/trace_coverage.json", trace_detail,
              "fine-grained trace coverage; evaluator-owned offline evidence")
    space.put("diagnostics/reasoning.json", summary.get("reasoning") or {},
              "agent reasoning diagnostic; evaluator-owned offline evidence")
    space.put("diagnostics/reachability.json", summary.get("reachability") or {},
              "candidate reachability replay; not agent-visible feedback")
    if initial_eval_row is None:
        initial_comparison = {
            "available": False,
            "winner": "unknown",
            "reason": "initial-results-dir was not provided",
        }
    else:
        current = outcome_record
        initial_comparison = {
            "available": True,
            "initial": initial_outcome,
            "current": current,
            "winner": "current" if _diagnosis_outcome_rank(current) > _diagnosis_outcome_rank(initial_outcome) else (
                "initial" if _diagnosis_outcome_rank(initial_outcome) > _diagnosis_outcome_rank(current) else "tie"
            ),
            "reason": "evaluator comparison only; diagnose mechanisms rather than attributing causality",
        }
    space.put(
        "initial_comparison.json",
        initial_comparison,
        "optional evaluator comparison against initial skill; unavailable must remain unknown",
    )
    space.copy_first([sample_dir / rel for rel in TRAJECTORY_CANDIDATES], "trajectory_metadata.txt",
                     "runner trajectory metadata or fallback log; use inputs/agent_sessions/ as the primary behavioral record")
    space.copy_files(checkpoint_dir, "checkpoint",
                     "checkpoint metadata: task/status, workspace listing, and analysis candidate provenance",
                     keep=CHECKPOINT_FILES)
    space.copy_glob_tree(sample_dir / "runs", "run_logs",
                         "adapter logs and model bridge logs for the coding-agent run",
                         patterns=RUN_LOG_PATTERNS)
    space.copy_tree(sample_dir / "submissions", "submissions",
                    "one directory per submitted candidate: request, candidate provenance, result, analysis, runtime output, and raw PoC bytes",
                    keep=SUBMISSION_FILES)
    # The agent-facing submit service may have been unavailable even though the
    # runner's one-shot reachability replay executed the same persisted PoC
    # successfully.  Give the diagnostician those durable replay records so it
    # can distinguish a submit-service failure from the candidate's observed
    # execution, without re-submitting or requiring the target image again.
    space.copy(sample_dir / "reachability_eval.json", "reachability_eval.json",
               "evaluator-owned per-candidate replay summary; not agent-visible feedback")
    space.copy_glob_tree(sample_dir / "reachability", "reachability",
                         "durable per-candidate replay traces: GDB stdout/stderr and hit ledgers; not agent-visible feedback",
                         patterns=(
                             "*/gdb_stdout.txt",
                             "*/gdb_stderr.txt",
                             "*/reachability_hits.json",
                         ))

    space.put("case_profile.json", diagnosis_case_profile(outcome_record, diagnostic_eval_row),
              "compact evaluator-owned result and diagnostic summaries selecting the diagnosis branch; copy it exactly, do not recalculate it")
    space.put("sample.json", {"sample_id": sample_id}, "the sample id to echo back")
    return space.finish("diagnose", "diagnostician.md")


def build_teacher_role(
    workspace: Path,
    packet: Path,
    diagnoses: list[dict[str, Any]],
    pools_view: dict[str, Any],
    teacher_evidence: dict[str, Any] | None = None,
) -> RoleRun:
    space = _Workspace(workspace)
    space.copy_packet(packet)
    space.put("batch_diagnoses.json", diagnoses, "this batch's per-sample diagnoses")
    space.put("pools.json", pools_view,
              "accumulated target-reproduction, false-positive, failure, and infrastructure diagnoses")
    space.put(
        "teacher_evidence.json",
        teacher_evidence or {
            "protocol": "teacher-evidence-view-v2",
            "sample_count": 0,
            "samples": [],
            "unavailable": "the caller did not provide an evaluator evidence view",
        },
        "ten-sample evidence view: bounded agent trace windows, candidate structure and visible feedback, evaluator reachability ladders, and Diagnostician explanations",
    )
    return space.finish("teacher", "teacher.md")


def build_curator_role(
    workspace: Path,
    packet: Path,
    candidate_updates: list[dict[str, Any]] | dict[str, Any],
    diagnoses: list[dict[str, Any]] | None = None,
    pools_view: dict[str, Any] | None = None,
) -> RoleRun:
    space = _Workspace(workspace)
    # A pre-findings caller passed pools as the fourth positional argument;
    # keep that historical call shape working while the explicit diagnoses
    # argument is now populated by the CLI.
    if isinstance(diagnoses, dict) and pools_view is None:
        pools_view = diagnoses
        diagnoses = None
    space.copy_packet(packet)
    # New Teacher output is an object containing both findings and proposals.
    # Keep the complete object for Curator; accepting a list preserves callers
    # and historical runs that predate the findings section.
    teacher_payload = (
        dict(candidate_updates)
        if isinstance(candidate_updates, dict)
        else {"candidate_updates": candidate_updates}
    )
    space.put("candidate_updates.json", teacher_payload,
              "the Teacher's findings and proposals with their evidence")
    space.put(
        "batch_diagnoses.json",
        diagnoses or [],
        "the same current diagnosis window the Teacher used; independently verify every accepted proposal here",
    )
    if pools_view is not None:
        space.put("pools.json", pools_view,
                  "the same earlier-batch historical pool the Teacher used")
    return space.finish("curator", "curator.md")


FINAL_BATCH_FILES = (
    ("evaluation.json", "batch deterministic outcome and diagnostics report"),
    ("diagnoses.json", "batch sample-level evidence abstractions"),
    ("teacher_evidence.json", "bounded ten-sample trace/candidate/reachability evidence supplied to Teacher"),
    ("teacher_candidate_updates.json", "batch Teacher proposals with evidence"),
    ("curator_decisions.json", "batch Curator decisions"),
    ("correction_decision.json", "batch Correction audit, if any"),
    ("applied_updates.json", "updates applied after this batch"),
    ("training_loop.json", "training-loop event metadata"),
)


def _copy_final_stage_inputs(
    space: _Workspace,
    *,
    initial_packet: Path,
    latest_packet: Path,
    run_dir: Path,
    through_batch: int,
    comparison_paths: list[Path] | None,
) -> None:
    space.copy_packet_as(initial_packet, "initial_skill",
                         "the starting skill packet before training",
                         snapshot_name="initial_lessons_snapshot.json")
    space.copy_packet_as(latest_packet, "latest_skill",
                         "the latest trained skill packet being curated",
                         snapshot_name="latest_lessons_snapshot.json")
    space.put("finalization_context.json", {
        "through_batch": through_batch,
        "initial_packet": str(initial_packet),
        "latest_packet": str(latest_packet),
    }, "metadata for this final-stage curation run")
    space.copy(run_dir / "batch_summary.json", "batch_summary.json",
               "baseline and batch ordering summary for the training split")
    space.copy(run_dir / "pools.json", "pools.json",
               "accumulated TRAIN success, failure, non-target-crash, and infrastructure pools")
    for batch_index in range(through_batch + 1):
        batch_dir = run_dir / f"batch_{batch_index:03d}"
        for name, description in FINAL_BATCH_FILES:
            space.copy(batch_dir / name, f"training_batches/batch_{batch_index:03d}/{name}", description)
    for index, path in enumerate(comparison_paths or [], start=1):
        if path.is_file():
            space.copy(path, f"comparisons/comparison_{index:02d}.json",
                       "optional held-out or validation comparison for final skill quality")


def build_final_teacher_role(
    workspace: Path,
    *,
    initial_packet: Path,
    latest_packet: Path,
    run_dir: Path,
    through_batch: int,
    comparison_paths: list[Path] | None = None,
) -> RoleRun:
    space = _Workspace(workspace)
    _copy_final_stage_inputs(
        space,
        initial_packet=initial_packet,
        latest_packet=latest_packet,
        run_dir=run_dir,
        through_batch=through_batch,
        comparison_paths=comparison_paths,
    )
    return space.finish("final_teacher", "final_teacher.md")


def build_final_curator_role(
    workspace: Path,
    *,
    initial_packet: Path,
    latest_packet: Path,
    run_dir: Path,
    through_batch: int,
    final_teacher_review: Path,
    comparison_paths: list[Path] | None = None,
) -> RoleRun:
    space = _Workspace(workspace)
    _copy_final_stage_inputs(
        space,
        initial_packet=initial_packet,
        latest_packet=latest_packet,
        run_dir=run_dir,
        through_batch=through_batch,
        comparison_paths=comparison_paths,
    )
    space.copy(final_teacher_review, "final_teacher_review.json",
               "Final Teacher lesson-level evidence review; primary curation input")
    return space.finish("final_curator", "final_curator.md")



def build_correction_role(
    workspace: Path,
    previous_packet: Path,
    current_packet: Path,
    batch_evaluation: dict[str, Any],
    batch_diagnoses: list[dict[str, Any]],
    applied_updates: dict[str, Any],
    pools_view: dict[str, Any],
    baseline_evaluations: list[dict[str, Any]] | None = None,
    rolling_comparison: dict[str, Any] | None = None,
) -> RoleRun:
    space = _Workspace(workspace)
    space.copy_packet_as(previous_packet, "previous_skill",
                         "the packet used before the tentative update",
                         snapshot_name="previous_lessons_snapshot.json")
    space.copy_packet_as(current_packet, "current_skill",
                         "the tentative/current packet being corrected",
                         snapshot_name="current_lessons_snapshot.json")
    space.put("batch_evaluation.json", batch_evaluation,
              "current batch execution/evaluator results for the tentative/current packet")
    space.put("batch_diagnoses.json", batch_diagnoses,
              "per-sample diagnoses explaining the current batch outcomes")
    space.put("applied_updates.json", applied_updates,
              "updates that produced the tentative/current packet, if available")
    space.put("pools.json", pools_view,
              "full accumulated target-reproduction, false-positive, failure, and infrastructure pools through this batch")
    if baseline_evaluations:
        space.put("baseline_evaluations.json", baseline_evaluations,
                  "optional baseline or initial-skill evaluations for comparison")
    if rolling_comparison is not None:
        space.put(
            "rolling_comparison.json",
            rolling_comparison,
            "recent same-sample initial-skill versus evolving-skill comparison used only for calibration or rollback",
        )
    return space.finish("correction", "correction.md")


def execute_role(
    run: RoleRun,
    *,
    harness: str = "codex",
    model: str,
    base_url: str,
    api_key_env: str,
    api_version: str = "2024-03-01-preview",
    reasoning_effort: str = "medium",
    max_output_tokens: int = 4096,
    timeout: int = 3600,
    dsh_command: str = "",
    dsh_provider: str = "deepseek-official",
) -> dict[str, Any]:
    """Run one role as an agent subsession and return its status plus artifact."""
    normalized_harness = (harness or "codex").strip().lower()
    if normalized_harness in {"deepseek_harness", "dsh"}:
        status = _execute_dsh_role(
            run,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
            timeout=timeout,
            dsh_command=dsh_command,
            dsh_provider=dsh_provider,
        )
    elif normalized_harness == "codex":
        status = _execute_codex_role(
            run,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            api_version=api_version,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
            timeout=timeout,
        )
    else:
        raise ValueError(f"unsupported role harness: {harness!r}")
    parsed = load_json_if_exists(run.output_file) if status["status"] == "ok" else None
    if status["status"] == "ok" and not isinstance(parsed, dict):
        status["status"] = "invalid_output"
        parsed = None
    # The workspace carries its own last outcome, so a later run can decide
    # whether to reuse, resume, or start over without consulting anything else.
    write_json(run.workspace / STATUS_NAME, {k: v for k, v in status.items() if k != "parsed"})
    status["parsed"] = parsed
    return status


def _execute_codex_role(
    run: RoleRun,
    *,
    model: str,
    base_url: str,
    api_key_env: str,
    api_version: str,
    reasoning_effort: str,
    max_output_tokens: int,
    timeout: int,
) -> dict[str, Any]:
    """Run one role as a Codex subsession."""
    codex_home = run.workspace / ".codex_home"
    codex_home.mkdir(parents=True, exist_ok=True)
    return run_codex_subsession(SubsessionSpec(
        name=run.role,
        workspace=run.workspace,
        prompt=run.prompt,
        output_file=run.output_file,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        api_version=api_version,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
        timeout=timeout,
        bridge="modelhub_crawl" if "modelhub" in base_url else "auto",
        log_dir=run.workspace / "logs",
        extra_env={"CODEX_HOME": str(codex_home)},
    ))


def _execute_dsh_role(
    run: RoleRun,
    *,
    model: str,
    base_url: str,
    api_key_env: str,
    reasoning_effort: str,
    timeout: int,
    max_output_tokens: int = 4096,
    dsh_command: str = "",
    dsh_provider: str = "deepseek-official",
) -> dict[str, Any]:
    """Run one role through DeepSeek Harness rather than Codex."""
    from harness_runtime.deepseek_harness.local import (
        DEFAULT_DSH_COMMAND,
        DEFAULT_DSH_NODE_ROOT,
        resolve_dsh_command,
        run_dsh,
    )

    logs = run.workspace / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    prompt_path = run.workspace / "PROMPT.txt"
    prompt_path.write_text(run.prompt, encoding="utf-8")

    dsh_home = run.workspace / ".dsh_home"
    dsh_effort = "high" if reasoning_effort == "medium" else reasoning_effort
    dsh_src = Path(__file__).resolve().parents[2] / "external" / "deepseek-harness"
    try:
        # Resolve before starting so the persisted status records the exact
        # cached launcher, never a misleading PATH-only `dsh` placeholder.
        resolved_dsh = resolve_dsh_command(
            dsh_src=dsh_src,
            node_root=DEFAULT_DSH_NODE_ROOT,
            dsh_command=dsh_command or DEFAULT_DSH_COMMAND,
        )
        resolved_dsh_string = " ".join(shlex.quote(part) for part in resolved_dsh)
        started_command = " ".join(
            shlex.quote(part)
            for part in [*resolved_dsh, "--profile", "headless", str(prompt_path)]
        )
        returncode, timed_out, stop_reason_hit, seconds = run_dsh(
            dsh_src=dsh_src,
            node_root=DEFAULT_DSH_NODE_ROOT,
            dsh_command=resolved_dsh_string,
            dsh_home=dsh_home,
            workspace=run.workspace,
            prompt_path=prompt_path,
            run_dir=logs,
            api_key=load_env_key(api_key_env),
            base_url=base_url,
            model=model,
            reasoning_effort=dsh_effort,
            # Keep role sessions on the same inexpensive default as the
            # reward runner; callers can opt into a larger role budget by
            # passing max_output_tokens explicitly.
            max_output_tokens=max_output_tokens,
            timeout=timeout,
            network_guard_bin=None,
            network_guard_so=None,
            provider=dsh_provider,
            # Roles inspect persisted evidence and write an artifact; unlike a
            # coding evaluation, their workspace intentionally has no
            # candidate submission interface.
            submission_preflight=False,
        )
    except Exception as exc:
        return {
            "subsession": run.role,
            "harness": "deepseek_harness",
            "status": "failed",
            "returncode": None,
            "workspace": str(run.workspace),
            "output_file": str(run.output_file),
            "stdout": str(logs / "dsh_stdout.txt"),
            "stderr": str(logs / "dsh_stderr.txt"),
            "command": locals().get("started_command", "DSH launcher resolution failed"),
            "error": str(exc),
        }
    if timed_out:
        status = "timeout"
    elif returncode != 0:
        status = "failed"
    elif not run.output_file.is_file():
        status = "missing_output"
    else:
        status = "ok"
    return {
        "subsession": run.role,
        "harness": "deepseek_harness",
        "status": status,
        "returncode": returncode,
        "workspace": str(run.workspace),
        "output_file": str(run.output_file),
        "stdout": str(logs / "dsh_stdout.txt"),
        "stderr": str(logs / "dsh_stderr.txt"),
        "command": started_command,
        "timed_out": timed_out,
        "stop_reason": stop_reason_hit,
        "seconds": seconds,
    }
