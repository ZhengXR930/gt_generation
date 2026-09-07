"""Abstract diagnosis views handed to skill-learning roles.

Diagnostician subsessions may inspect raw evaluator and GT artifacts. Teacher,
Curator, and Correction should not consume those raw coordinates directly: when
stage labels, file/function/line locations, or exact sample facts leak into the
learning view, later roles tend to copy them into lessons. This module keeps the
learning payload focused on agent-observable behavior.
"""
from __future__ import annotations

import re
from typing import Any

TEXT_FIELDS = (
    "issue_alignment_diagnosis",
    "reasoning_diagnosis",
    "reachability_diagnosis",
    "submission_diagnosis",
    "candidate_source_diagnosis",
    "stage_summary",
    "evidence_excerpt",
)

ABSTRACT_FIELDS = (*TEXT_FIELDS, "issue_description")

_LEARNING_KEYS = (
    "sample_id",
    "outcome",
    "issue_description",
    "vulnerability_type",
    *TEXT_FIELDS,
)

_REPLACEMENTS = (
    (re.compile(r"\bR[1-5]_[A-Za-z0-9_]+\b"), "runtime milestone"),
    (re.compile(r"\bR[1-5]\b"), "runtime milestone"),
    (re.compile(r"\bParser\b", re.IGNORECASE), "input admission"),
    (re.compile(r"\bSource\b", re.IGNORECASE), "input-controlled behavior"),
    (re.compile(r"\bRoot[ -]?Cause\b", re.IGNORECASE), "vulnerable condition"),
    (re.compile(r"\bSink\b", re.IGNORECASE), "sensitive operation"),
    (re.compile(r"\bTrigger\b", re.IGNORECASE), "observable failure"),
    (re.compile(r"\bGT\b"), "target"),
)

_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[./~]?[-A-Za-z0-9_]+/)+(?:[-A-Za-z0-9_]+\.)"
    r"(?:c|cc|cpp|cxx|h|hh|hpp|java|rs|go|py|php|js|ts|m|mm|S|asm)"
    r"(?::\d+)?"
)
_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])[-A-Za-z0-9_]+\."
    r"(?:c|cc|cpp|cxx|h|hh|hpp|java|rs|go|py|php|js|ts|m|mm|S|asm)"
    r"(?::\d+)?"
)
_URL_RE = re.compile(r"https?://\S+")
_COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_LINE_RE = re.compile(r"\b(?:line|lines)\s+\d+(?:\s*[-–]\s*\d+)?\b", re.IGNORECASE)
_BACKTICK_PATH_RE = re.compile(r"`[^`]*(?:/|\.(?:c|cc|cpp|h|hpp|py|java|rs|go|php))[^`]*`")

_CODE_SYMBOL_RE = re.compile(
    r"\b(?!arvo_\d+\b)(?!secbench_\w+\b)(?!nvd_CVE_\w+\b)(?!osv_\w+\b)"
    r"(?:"
    r"[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_:<>~]*"
    r"|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r"|[A-Z]{2,}_[A-Za-z0-9_]+"
    r"|[A-Za-z]+[A-Za-z0-9]*[A-Z][A-Za-z0-9_]*"
    r")\b"
)
_BACKTICK_SYMBOL_RE = re.compile(r"`[^`]*[A-Za-z_][A-Za-z0-9_:<>~]*[^`]*`")
_HYPHEN_ARTIFACT_RE = re.compile(r"\b[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)+\.(?:c|cc|cpp|cxx|h|hh|hpp|java|rs|go|py|php|js|ts|m|mm|S|asm)(?::\d+)?")
_PARTIAL_ARTIFACT_RE = re.compile(r"\b[A-Za-z0-9_]+-source file\w*(?::\d+)?")
_WORKSPACE_PATH_FRAGMENT_RE = re.compile(r"\b(?:repo-vul|src-vul|test|tests|work|src)/[^\s,.;)]+")
_HARFBUZZ_TARGET_RE = re.compile(r"\bhb-[A-Za-z0-9_.:/+-]+")
_WHITESPACE_RE = re.compile(r"\s+")


def abstract_text(value: Any) -> str:
    """Remove evaluator/GT coordinates while preserving behavioral meaning."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = _URL_RE.sub("public issue reference", text)
    text = _BACKTICK_PATH_RE.sub("source artifact", text)
    text = _BACKTICK_SYMBOL_RE.sub("project symbol", text)
    text = _PATH_RE.sub("source file", text)
    text = _WORKSPACE_PATH_FRAGMENT_RE.sub("source artifact", text)
    text = _HYPHEN_ARTIFACT_RE.sub("source file", text)
    text = _FILE_RE.sub("source file", text)
    text = _HARFBUZZ_TARGET_RE.sub("project fuzz target", text)
    text = _LINE_RE.sub("a specific line", text)
    text = _COMMIT_RE.sub("commit", text)
    for pattern, replacement in _REPLACEMENTS:
        text = pattern.sub(replacement, text)
    text = _CODE_SYMBOL_RE.sub("project symbol", text)
    text = _PARTIAL_ARTIFACT_RE.sub("source file", text)
    text = _HARFBUZZ_TARGET_RE.sub("project fuzz target", text)
    text = _WORKSPACE_PATH_FRAGMENT_RE.sub("source artifact", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def learning_diagnosis(diagnosis: dict[str, Any], outcome_record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the abstract per-sample diagnosis consumed by learning roles."""
    out: dict[str, Any] = {}
    for key in _LEARNING_KEYS:
        if key not in diagnosis:
            continue
        value = diagnosis.get(key)
        if value in (None, ""):
            continue
        if key in ABSTRACT_FIELDS:
            out[key] = abstract_text(value)
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
