"""Compact, provenance-preserving evidence for the Teacher role.

The diagnostician is intentionally given the complete trajectory.  The Teacher
needs a different view: enough of each rollout to explain why one candidate
went farther than another, without having to ingest ten multi-megabyte JSONL
sessions.  This module materializes that view from the evaluator-owned result
directory and keeps the raw artifact paths for follow-up inspection.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .outcome import (
    OBSERVED_STAGE_ORDER,
    candidate_occurrence_records,
    candidate_replay_observation,
    observed_candidate_stage,
)


_SESSION_PATTERNS = (
    "dsh_home/sessions-jsonl/**/session*.jsonl",
    "dsh_home/sessions/**/session*.jsonl",
    "file/sessions/**/events/*.json",
    "file/sessions/**/event_cache/*.json",
    "**/*stdout*.jsonl",
    "**/*transcript*.jsonl",
    "**/*conversation*.jsonl",
    "**/agent.log",
)
_FEEDBACK_RE = re.compile(
    r"(?:asan|sanitizer|crash|segmentation|exit code|returned|response|feedback|clean|error)",
    re.IGNORECASE,
)
_HYPOTHESIS_RE = re.compile(
    r"\b(?:hypothes|variant|alternative|switch|instead|try|attempt|candidate|revision|revise|change|new approach)\b",
    re.IGNORECASE,
)
_MAX_TEXT = 900
_MAX_TRACE_WINDOWS = 40


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _clip(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + " …[truncated]"


def _text_fragments(value: Any) -> list[str]:
    """Extract human-readable content from session-v4 and generic events."""
    out: list[str] = []
    if isinstance(value, str):
        if value.strip():
            out.append(value)
    elif isinstance(value, list):
        for item in value:
            out.extend(_text_fragments(item))
    elif isinstance(value, dict):
        # Do not stringify the whole event: arguments and tool output are the
        # useful parts and this avoids duplicating timestamps/metadata.
        for key in ("text", "content", "arguments", "output", "stdout", "stderr", "message", "data"):
            if key in value:
                out.extend(_text_fragments(value[key]))
    return out


def _session_lines(sample_dir: Path) -> list[str]:
    checkpoint = sample_dir / "checkpoint"
    files: set[Path] = set()
    for pattern in _SESSION_PATTERNS:
        files.update(path for path in checkpoint.glob(pattern) if path.is_file())
    lines: list[str] = []
    for path in sorted(files):
        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in raw_lines:
            try:
                event = json.loads(raw)
            except ValueError:
                text = raw
            else:
                fragments = _text_fragments(event)
                text = " | ".join(fragments)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                lines.append(text)
    return lines


def _trace_windows(lines: list[str], candidate_count: int) -> list[dict[str, Any]]:
    """Return bounded before/after snippets around submit-like events."""
    def is_submission_boundary(index: int, line: str) -> bool:
        # Prompts and skill text mention ``submit.sh`` too.  A boundary must
        # look like an actual tool command/returned response, and the initial
        # prompt region is never considered.
        if index < 12:
            return False
        lowered = line.lower()
        if (
            line.lstrip().startswith('{"command":')
            and re.search(r"(?:bash|sh)\s+(?:\./)?submit\.sh\s+", lowered)
        ):
            return True
        return False

    markers = [index for index, line in enumerate(lines) if is_submission_boundary(index, line)]
    # If the harness did not preserve a literal submit command, feedback lines
    # still anchor the same behavioral boundary.
    if not markers:
        markers = [index for index, line in enumerate(lines) if _FEEDBACK_RE.search(line) and index >= 12]
    windows: list[dict[str, Any]] = []
    limit = max(8, min(_MAX_TRACE_WINDOWS, candidate_count * 3 if candidate_count else 8))
    for ordinal, index in enumerate(markers[:limit], 1):
        before = [_clip(line) for line in lines[max(0, index - 2):index] if line]
        after = [_clip(line) for line in lines[index:min(len(lines), index + 3)] if line]
        windows.append({"event_index": index, "ordinal": ordinal, "before": before, "after": after})
    # Preserve the relationship to evaluator candidates even when the agent
    # made fewer or more textual submit calls than persisted unique PoCs.
    for index, window in enumerate(windows):
        window["candidate_sequence"] = min(index + 1, max(1, candidate_count))
    return windows


def _hypothesis_markers(lines: list[str]) -> list[dict[str, Any]]:
    """Extract agent-authored places where it describes a changed approach."""
    markers: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if index < 12 or not _HYPOTHESIS_RE.search(line):
            continue
        # Source dumps contain many occurrences of "attempt"/"change"; keep
        # prose and command descriptions, not giant file contents.
        if len(line) > 1800:
            continue
        markers.append({"event_index": index, "text": _clip(line, 1200)})
    return markers[:30]


def _analysis_summary(path: Path) -> Any:
    value = _read_json(path)
    if value is None:
        return None
    def bound(item: Any, depth: int = 0) -> Any:
        if depth > 3:
            return _clip(item, 500)
        if isinstance(item, dict):
            return {str(k): bound(v, depth + 1) for k, v in list(item.items())[:30]}
        if isinstance(item, list):
            return [bound(v, depth + 1) for v in item[:30]]
        if isinstance(item, str):
            return _clip(item, 1200)
        return item
    # Analysis is agent-authored evidence. Keep its claims, but bound the
    # prompt-facing copy; the original file remains referenced below.
    return bound(value)


def _binary_shape(path: Path) -> dict[str, Any] | None:
    """Describe a candidate without copying its complete PoC into the prompt."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return {"size_bytes": 0, "prefix_hex": "", "suffix_hex": ""}
    printable = sum(32 <= byte < 127 or byte in (9, 10, 13) for byte in data)
    return {
        "size_bytes": len(data),
        "prefix_hex": data[:32].hex(),
        "suffix_hex": data[-32:].hex(),
        "distinct_byte_values": len(set(data)),
        "zero_byte_count": data.count(0),
        "printable_ratio": round(printable / len(data), 3),
    }


def _feedback(sample_dir: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    attempt = str(candidate.get("attempt_id") or "")
    subdir = sample_dir / "submissions" / attempt
    runtime = subdir / "runtime_output.txt"
    result = _read_json(subdir / "result.json")
    output = ""
    if runtime.is_file():
        try:
            output = _clip(runtime.read_text(encoding="utf-8", errors="replace"), 1800)
        except OSError:
            pass
    result_view = None
    if isinstance(result, dict):
        result_view = {
            key: result.get(key)
            for key in (
                "task_id", "attempt_id", "poc_id", "exit_code", "trace_valid", "trace_error",
            )
            if result.get(key) not in (None, "", [], {})
        }
    elif isinstance(result, (str, int, float, bool)):
        result_view = result
    return {
        "execution_status": candidate.get("execution_status"),
        "original_exit_code": candidate.get("original_exit_code"),
        "execution_error": candidate.get("execution_error"),
        "runtime_output": output,
        "result": result_view,
    }


def _ladder(candidate: dict[str, Any]) -> dict[str, Any]:
    reach = candidate.get("location_reachability")
    if not isinstance(reach, dict):
        reach = {}
    return {
        "R1_input_admitted": bool(reach.get("R1_input_admitted", reach.get("R1_parser_admitted"))),
        "R2_source_reached": bool(reach.get("R2_source_reached")),
        "R3_root_cause_reached": bool(reach.get("R3_root_cause_reached")),
        "R4_sink_reached": bool(reach.get("R4_sink_reached")),
        "R5_triggered": bool(reach.get("R5_sanitizer_triggered", candidate.get("target_vulnerability_triggered"))),
        "depth": reach.get("reachability_depth") or candidate.get("reachability_depth"),
        "failure_stage": reach.get("failure_stage") or candidate.get("failure_stage"),
        "target_vulnerability_triggered": bool(candidate.get("target_vulnerability_triggered")),
    }


def _stage_code(stage: Any) -> str:
    value = str(stage or "").strip()
    if value.startswith("R") and len(value) > 1 and value[1].isdigit():
        return value.split("_", 1)[0]
    return {
        "Parser": "R1",
        "Source": "R2",
        "RootCause": "R3",
        "Root Cause": "R3",
        "Sink": "R4",
        "Trigger": "R5",
    }.get(value, "R0")


def _diagnostic_view(diagnosis: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(diagnosis, dict):
        return {}
    keys = (
        "primary_failure_type", "failure_source", "harness_group",
        "vulnerability_group", "candidate_sequence", "progress_events",
        "turning_events", "initial_comparison", "one_sentence_explanation",
        "outcome", "why_no_submission", "why_not_reached", "why_false_positive",
        "why_stopped", "why_succeeded", "submission_review", "visible_signals",
        "evidence", "uncertainty",
        "selected_attempt", "behavior_summary",
    )
    view = {key: diagnosis[key] for key in keys if diagnosis.get(key) not in (None, "", [], {})}
    sequence = view.get("candidate_sequence")
    if isinstance(sequence, list):
        canonical: list[Any] = []
        for raw in sequence:
            if not isinstance(raw, dict):
                canonical.append(raw)
                continue
            item = dict(raw)
            if not item.get("observed_stage") and item.get("stage"):
                item["observed_stage"] = item["stage"]
            if not item.get("stage_delta_from_previous") and item.get("stage_change"):
                item["stage_delta_from_previous"] = item["stage_change"]
            canonical.append(item)
        view["candidate_sequence"] = canonical
    return view


def build_teacher_evidence(
    evaluation: dict[str, Any],
    diagnoses: Iterable[dict[str, Any]] = (),
    *,
    max_samples: int | None = None,
) -> dict[str, Any]:
    """Build the evidence packet consumed by Teacher.

    The evaluator remains authoritative for reachability.  Trace and analysis
    are labeled as agent-visible/agent-authored so the Teacher can use them for
    causal comparison without treating them as ground truth.
    """
    source = Path(str(evaluation.get("source_results_dir") or ""))
    diagnosis_by_sample = {
        str(item.get("sample_id")): item for item in diagnoses if isinstance(item, dict)
    }
    rows = evaluation.get("rows") or []
    samples: list[dict[str, Any]] = []
    for row in rows[:max_samples] if max_samples else rows:
        if not isinstance(row, dict):
            continue
        sample_id = str(row.get("sample_id") or "")
        if not sample_id:
            continue
        sample_dir = source / sample_id
        runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
        candidates: list[dict[str, Any]] = []
        manifest = _read_json(sample_dir / "manifest.json") or {}
        submission_attempts: list[dict[str, Any]] = []
        if isinstance(manifest, dict):
            poc_generation = manifest.get("poc_generation")
            if isinstance(poc_generation, dict) and isinstance(poc_generation.get("submission_attempts"), list):
                submission_attempts = poc_generation["submission_attempts"]
            elif isinstance(manifest.get("submission_attempts"), list):
                submission_attempts = manifest["submission_attempts"]
        raw_candidates = candidate_occurrence_records(row, submission_attempts)
        for candidate in raw_candidates:
            attempt = str(candidate.get("attempt_id") or "")
            subdir = sample_dir / "submissions" / attempt
            poc = subdir / "poc.bin"
            candidates.append({
                "attempt_id": attempt,
                "sequence_in_run": candidate.get("sequence_in_run"),
                "poc_sha256": candidate.get("poc_sha256"),
                "submission_occurrence": bool(candidate.get("submission_occurrence")),
                "is_duplicate_submission": bool(candidate.get("is_duplicate_submission")),
                "duplicate_of_sequence": candidate.get("duplicate_of_sequence"),
                "duplicate_of_attempt_id": candidate.get("duplicate_of_attempt_id"),
                "unique_representative_attempt_id": candidate.get("unique_representative_attempt_id"),
                "poc_size_bytes": poc.stat().st_size if poc.is_file() else None,
                "structural_summary": {
                    "agent_analysis": _analysis_summary(subdir / "analysis.json"),
                    "binary_shape": _binary_shape(poc),
                },
                "visible_feedback": _feedback(sample_dir, candidate),
                "reachability_ladder": _ladder(candidate),
                # This is the evaluator-visible behavior axis used for
                # progress comparisons; it is not target-ladder alignment.
                "observed_stage": observed_candidate_stage(candidate),
                "replay_observation": candidate_replay_observation(candidate),
                "artifact_paths": {
                    "analysis": str((subdir / "analysis.json").relative_to(sample_dir)) if (subdir / "analysis.json").is_file() else None,
                    "runtime_output": str((subdir / "runtime_output.txt").relative_to(sample_dir)) if (subdir / "runtime_output.txt").is_file() else None,
                },
            })
        trace_lines = _session_lines(sample_dir)
        det = row.get("deterministic_outcome") if isinstance(row.get("deterministic_outcome"), dict) else {}
        deepest_stage = det.get("deepest_stage")
        observed_stages = [item.get("observed_stage") for item in candidates]
        max_observed_stage = max(
            (stage for stage in observed_stages if stage in OBSERVED_STAGE_ORDER),
            key=OBSERVED_STAGE_ORDER.index,
            default="none",
        )
        samples.append({
            "sample_id": sample_id,
            "outcome": det.get("outcome"),
            "evaluator_summary": {
                key: det.get(key)
                for key in (
                    "triggered", "false_positive", "false_positive_pocs",
                    "submitted_unique_pocs", "executed_candidates", "first_failed_stage",
                )
                if det.get(key) not in (None, "", [], {})
            },
            "sample_max_reachability": {
                "target_stage": deepest_stage or "",
                "stage": deepest_stage or "",
                "depth": _stage_code(deepest_stage) if deepest_stage else max((_stage_code(c["reachability_ladder"].get("depth")) for c in candidates), default="R0"),
                "observed_stage": max_observed_stage,
            },
            "candidate_count": len(candidates),
            "candidates": candidates,
            "agent_trace": {
                "source_session_files": [str(path.relative_to(sample_dir / "checkpoint")) for pattern in _SESSION_PATTERNS for path in (sample_dir / "checkpoint").glob(pattern) if path.is_file()],
                "submission_windows": _trace_windows(trace_lines, len(candidates)),
                "hypothesis_change_markers": _hypothesis_markers(trace_lines),
                "visibility": "agent transcript excerpts around submit/feedback boundaries; not evaluator ground truth",
            },
            "diagnostician": _diagnostic_view(diagnosis_by_sample.get(sample_id)),
        })
    return {
        "protocol": "teacher-evidence-view-v2",
        "batch_index": evaluation.get("batch_index"),
        "source_results_dir": str(source),
        "sample_count": len(samples),
        "samples": samples,
        "authority": {
            "reachability": "evaluator-owned candidate reachability_ladder and sample_max_reachability",
            "trace": "agent-visible excerpts, useful for comparing hypotheses but not proof of a path",
            "diagnosis": "diagnostician explanations with uncertainty preserved",
        },
    }
