"""Small, provenance-preserving diagnosis views for skill-learning roles.

The evaluator supplies ``case``: the deterministic result branch and compact
execution facts.  The diagnostician supplies only explanations.  This keeps a
Teacher from treating a model's guessed reachability labels as ground truth and
keeps the future agent's observable signals separate from offline evidence.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .outcome import OBSERVED_STAGE_ORDER, ordered_candidate_sequence

DIAGNOSIS_SECTIONS = (
    "case",
    "primary_failure_type",
    "failure_source",
    "harness_group",
    "vulnerability_group",
    "candidate_sequence",
    "progress_events",
    "turning_events",
    "initial_comparison",
    "one_sentence_explanation",
    "why_no_submission",
    "why_not_reached",
    "why_false_positive",
    "why_stopped",
    "why_succeeded",
    "submission_review",
    "visible_signals",
    "evidence",
    "uncertainty",
)

# Retained solely to read older pool artifacts during the schema migration.
RUBRIC_FINDINGS_KEY = "rubric_findings"
RUBRIC_CATEGORIES = (
    "submission_starvation",
    "feedback_loop_failure",
    "false_positive_stop",
    "target_mismatch",
    "over_investigation",
    "artifact_misalignment",
)

_LEGACY_DIAGNOSIS_KEYS = (
    "issue_description", "vulnerability_type", "outcome_evidence",
    "executable_progress_evidence", "semantic_explanatory_evidence",
    "behavioral_evidence", "outcome_diagnosis", "rubric_findings",
)
_LEARNING_KEYS = ("sample_id", "outcome", *DIAGNOSIS_SECTIONS, *_LEGACY_DIAGNOSIS_KEYS)

_REPLACEMENTS = (
    (re.compile(r"\bR0\b"), "no target-path progress"),
    (re.compile(r"\bR1(?:_[A-Za-z0-9_]+)?\b"), "accepted input"),
    (re.compile(r"\bR2(?:_[A-Za-z0-9_]+)?\b"), "entered issue-relevant path"),
    (re.compile(r"\bR3(?:_[A-Za-z0-9_]+)?\b"), "reached vulnerable condition"),
    (re.compile(r"\bR4(?:_[A-Za-z0-9_]+)?\b"), "reached sensitive operation"),
    (re.compile(r"\bR5(?:_[A-Za-z0-9_]+)?\b"), "produced observable failure"),
    (re.compile(r"\bParser\b"), "input admission"),
    (re.compile(r"\bSource\b"), "issue-relevant path"),
    (re.compile(r"\bRoot[ -]?Cause\b"), "vulnerable condition"),
    (re.compile(r"\bSink\b"), "sensitive operation"),
    (re.compile(r"\bTrigger\b"), "observable failure"),
    (re.compile(r"\bGT\b"), "target"),
)
_WHITESPACE_RE = re.compile(r"\s+")


def abstract_text(value: Any) -> str:
    """Remove evaluator rubric tokens while retaining behavioral evidence."""
    text = str(value or "").strip()
    if not text:
        return ""
    for pattern, replacement in _REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def abstract_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): abstract_value(item)
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [abstract_value(item) for item in value if item not in (None, "", [], {})]
    if isinstance(value, str):
        return abstract_text(value)
    return value


def learning_diagnosis(diagnosis: dict[str, Any], outcome_record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the compact case-plus-explanation record used for training."""
    out: dict[str, Any] = {}
    for key in _LEARNING_KEYS:
        value = diagnosis.get(key)
        if value in (None, "", [], {}):
            continue
        out[key] = abstract_value(value) if key in DIAGNOSIS_SECTIONS or key in _LEGACY_DIAGNOSIS_KEYS else value
    if outcome_record:
        out["outcome"] = outcome_record.get("outcome") or out.get("outcome")
    return out


def learning_evaluation(eval_report: dict[str, Any]) -> dict[str, Any]:
    """Small evaluator view for Correction without raw stage labels or traces."""
    rows = []
    for row in eval_report.get("rows") or []:
        progress_row = row
        source = eval_report.get("source_results_dir")
        sample_id = str(row.get("sample_id") or "") if isinstance(row, dict) else ""
        if source and sample_id and isinstance(row, dict):
            try:
                manifest = json.loads((Path(str(source)) / sample_id / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                manifest = None
            if isinstance(manifest, dict):
                generation = manifest.get("poc_generation")
                attempts = generation.get("submission_attempts") if isinstance(generation, dict) else None
                if not isinstance(attempts, list):
                    attempts = manifest.get("submission_attempts")
                if isinstance(attempts, list):
                    progress_row = dict(row)
                    progress_runtime = dict((row.get("runtime") or {}))
                    progress_runtime["submission_attempts"] = attempts
                    progress_row["runtime"] = progress_runtime
        det = row.get("deterministic_outcome")
        if not isinstance(det, dict):
            det = row
        sequence = ordered_candidate_sequence(progress_row)
        observed_max = max(
            (item.get("observed_stage") for item in sequence if item.get("observed_stage") in OBSERVED_STAGE_ORDER),
            key=OBSERVED_STAGE_ORDER.index,
            default="none",
        )
        target_max = max(
            (item.get("deepest_stage") for item in sequence if item.get("deepest_stage") in OBSERVED_STAGE_ORDER),
            key=OBSERVED_STAGE_ORDER.index,
            default="none",
        )
        rows.append({
            "sample_id": row.get("sample_id"),
            "outcome": det.get("outcome"),
            "triggered": bool(det.get("triggered")),
            "submission_success": bool(det.get("submission_success")),
            "submitted_unique_pocs": det.get("submitted_unique_pocs"),
            "executed_candidates": det.get("executed_candidates"),
            "false_positive": bool(det.get("false_positive")),
            "false_positive_pocs": int(det.get("false_positive_pocs") or 0),
            "submitted_occurrences": len(sequence),
            "candidate_progress": {
                "target_stage": target_max,
                "observed_stage": observed_max,
                "sequence": [
                    {
                        "sequence": item.get("sequence"),
                        "observed_stage": item.get("observed_stage"),
                        "stage_delta_from_previous": item.get("stage_delta_from_previous"),
                        "target_stage_delta_from_previous": item.get("target_stage_delta_from_previous"),
                        "is_duplicate_submission": item.get("is_duplicate_submission", False),
                    }
                    for item in sequence
                ],
            },
            "progress_summary": _progress_summary(det),
        })
    return {
        "protocol": "abstract-learning-evaluation-v1",
        "batch_index": eval_report.get("batch_index"),
        "rows": rows,
    }


def _progress_summary(det: dict[str, Any]) -> str:
    if det.get("triggered"):
        return "candidate reproduced the target observable failure"
    outcome = det.get("outcome")
    submitted = int(det.get("submitted_unique_pocs") or 0)
    if submitted <= 0:
        return "no candidate was submitted for runtime feedback"
    if det.get("false_positive"):
        return "candidate produced crash evidence that did not match the target issue"
    if outcome == "partial":
        return "candidate made runtime progress but did not reproduce the target observable failure"
    return "candidate did not produce useful target feedback"
