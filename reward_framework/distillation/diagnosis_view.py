"""Abstract diagnosis views handed to skill-learning roles.

Diagnostician subsessions may inspect raw evaluator and GT artifacts. Teacher,
Curator, and Correction should not consume evaluator ladder terms directly.
This module keeps diagnosis payloads in behavior language without erasing useful
project, sanitizer, input-format, or candidate-family terms.
"""
from __future__ import annotations

import re
from typing import Any

BEHAVIOR_SECTIONS = (
    "search_behavior",
    "candidate_behavior",
    "feedback_behavior",
    "outcome_diagnosis",
)

_LEARNING_KEYS = (
    "sample_id",
    "outcome",
    "issue_description",
    "vulnerability_type",
    *BEHAVIOR_SECTIONS,
    "retry_recommendation",
)

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
    """Rewrite evaluator-only terms while preserving technical behavior signal."""
    text = str(value or "").strip()
    if not text:
        return ""
    for pattern, replacement in _REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def abstract_value(value: Any) -> Any:
    """Recursively abstract strings while preserving JSON structure."""
    if isinstance(value, dict):
        return {
            str(key): abstract_value(item)
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [abstract_value(item) for item in value if item not in (None, "", [], {})]
    return abstract_text(value)


def learning_diagnosis(diagnosis: dict[str, Any], outcome_record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the abstract per-sample diagnosis consumed by learning roles."""
    out: dict[str, Any] = {}
    for key in _LEARNING_KEYS:
        if key not in diagnosis:
            continue
        value = diagnosis.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "issue_description":
            out[key] = abstract_text(value)
        elif key in BEHAVIOR_SECTIONS or key == "retry_recommendation":
            out[key] = abstract_value(value)
        else:
            out[key] = value
    if outcome_record:
        out["outcome"] = outcome_record.get("outcome") or out.get("outcome")
    return out


def learning_evaluation(eval_report: dict[str, Any]) -> dict[str, Any]:
    """Small evaluator view for Correction without raw stage labels or traces."""
    rows = []
    for row in eval_report.get("rows") or []:
        det = row.get("deterministic_outcome") or {}
        rows.append({
            "sample_id": row.get("sample_id"),
            "outcome": det.get("outcome"),
            "triggered": bool(det.get("triggered")),
            "submission_success": bool(det.get("submission_success")),
            "submitted_unique_pocs": det.get("submitted_unique_pocs"),
            "executed_candidates": det.get("executed_candidates"),
            "false_positive": bool(det.get("false_positive")),
            "false_positive_pocs": int(det.get("false_positive_pocs") or 0),
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
