"""Deterministic PoC deduplication for persisted submission attempts."""

from __future__ import annotations

from typing import Any


def deduplicate_submission_attempts(
    attempts: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Group equal PoC bytes and retain the last attempt as representative.

    The caller keeps the immutable attempt ledger.  This function builds the
    evaluation view: reachability executes one representative per PoC hash, and
    the representative's analysis artifact is the last analysis submitted for
    those exact bytes.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for index, attempt in enumerate(attempts, 1):
        poc_hash = str(attempt.get("poc_hash") or "").strip()
        # A missing hash must never merge unrelated submissions.
        key = poc_hash or f"missing-hash:{attempt.get('attempt_id') or index}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(attempt)

    representatives = []
    for key in order:
        occurrences = groups[key]
        latest = occurrences[-1]
        result_path = str(latest.get("result_path") or "").rstrip("/")
        representative = {
            "poc_sha256": (
                str(latest.get("poc_hash")) if latest.get("poc_hash") else None
            ),
            "occurrence_count": len(occurrences),
            "attempt_ids": [
                str(item.get("attempt_id"))
                for item in occurrences
                if item.get("attempt_id")
            ],
            "representative_attempt_id": latest.get("attempt_id"),
            "representative_sequence_in_run": latest.get("sequence_in_run"),
            "representative_analysis_path": (
                latest.get("analysis_path")
                or (f"{result_path}/analysis.json" if result_path else None)
            ),
            "representative_poc_path": (
                latest.get("poc_path")
                or (f"{result_path}/poc.bin" if result_path else None)
            ),
            "representative_runtime_output_path": (
                latest.get("runtime_output_path")
                or (f"{result_path}/runtime_output.txt" if result_path else None)
            ),
            "representative_vul_exit_code": latest.get("vul_exit_code"),
        }
        representatives.append(representative)

    total = len(attempts)
    unique = len(representatives)
    duplicates = total - unique
    stats = {
        "scope": "within_model_sample",
        "key": "poc_sha256",
        "representative_policy": "last_submission_analysis",
        "total_poc_submissions": total,
        "deduplicated_poc_count": unique,
        "duplicate_poc_submissions": duplicates,
        "deduplicated_ratio": (
            round(unique / total, 6) if total else None
        ),
        "duplicate_ratio": (
            round(duplicates / total, 6) if total else None
        ),
    }
    return stats, representatives
