"""Deterministic outcome classification for distillation batches.

The evaluator already produces hard signals: whether a submitted candidate
triggered the target vulnerability, and how deep it got through the R1..R5
location-reachability ladder. Those decide `outcome` and `first_failed_stage`;
the diagnostician is given that verdict and asked only to explain it.
"""
from __future__ import annotations

from typing import Any

STAGE_ORDER = ("Parser", "Source", "Root Cause", "Sink", "Trigger")

_LADDER = (
    ("R1_input_admitted", "Parser"),
    ("R2_source_reached", "Source"),
    ("R3_root_cause_reached", "Root Cause"),
    ("R4_sink_reached", "Sink"),
    ("R5_sanitizer_triggered", "Trigger"),
)


def candidate_ladder(candidate: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (deepest_reached_stage, first_failed_stage) for one candidate.

    A ``None`` rung means the evaluator could not observe that stage, so no
    failure is attributed beyond it.
    """
    reach = candidate.get("location_reachability") or {}
    reached: str | None = None
    for key, stage in _LADDER:
        value = reach.get(key)
        if value is True:
            reached = stage
        elif value is False:
            return reached, stage
        else:
            return reached, None
    return reached, None


def classify_outcome(eval_row: dict[str, Any] | None) -> dict[str, Any]:
    """Map one evaluator row to a deterministic outcome record."""
    runtime = ((eval_row or {}).get("runtime") or {}) if isinstance(eval_row, dict) else {}
    candidates = [item for item in (runtime.get("candidates") or []) if isinstance(item, dict)]
    executed = [item for item in candidates if item.get("execution_status") == "executed"]
    submitted = int(runtime.get("submitted_unique_pocs") or 0)
    submission = runtime.get("submission_outcome") or {}
    unavailable = str(runtime.get("unavailable") or "")

    def record(outcome: str, *, deepest: str | None = None, failed: str | None = None, reason: str = "") -> dict[str, Any]:
        crashed_pocs = int(submission.get("crashed_pocs") or submission.get("triggered_pocs") or 0)
        target_triggered = outcome == "success"
        false_positive_pocs = crashed_pocs if crashed_pocs and not target_triggered else 0
        row = {
            "protocol": "deterministic-outcome-v1",
            "submitted_unique_pocs": submitted,
            "submission_success": submission.get("success"),
            "submission_triggered": submission.get("triggered"),
            "submission_triggered_pocs": submission.get("triggered_pocs"),
            "submission_crashed_pocs": submission.get("crashed_pocs"),
            "false_positive": bool(false_positive_pocs),
            "false_positive_pocs": false_positive_pocs,
            "executed_candidates": len(executed),
            "triggered": target_triggered,
            "outcome": outcome,
            "deepest_stage": deepest,
            "first_failed_stage": failed,
        }
        if reason:
            row["reason"] = reason
        return row

    if any(item.get("target_vulnerability_triggered") is True for item in candidates):
        return record("success", deepest="Trigger")
    if not candidates:
        if "reachability has not been executed" in unavailable:
            return record("infrastructure", reason="reachability not executed")
        if submitted == 0:
            return record("failure", reason="no candidate was submitted")
        return record("infrastructure", reason=unavailable or "no reachability candidates")
    if not executed:
        return record("infrastructure", reason="no candidate executed under the reachability harness")

    # Credit the candidate that got furthest; keep an attribution if any produced one.
    reached, failed = max((candidate_ladder(item) for item in executed), key=lambda pair: _depth(pair[0]))
    if failed is None:
        failed = next((item[1] for item in map(candidate_ladder, executed) if item[1]), None)
    if reached:
        return record("partial", deepest=reached, failed=failed)
    return record("failure", failed=failed,
                  reason="" if failed else "candidates executed but no reachability rung was observed")


def diagnostics_summary(eval_row: dict[str, Any] | None) -> dict[str, Any]:
    """The headline numbers of all three diagnostics, side by side.

    The full rows are also handed to the diagnostician, but they are large -
    per-step match tables and per-candidate ladders - and the comparison that
    produces a diagnosis is between the three headline views, not inside any one
    of them. What the agent *wrote* is scored by trace coverage and reasoning;
    what it *did* is recorded by reachability.
    """
    row = eval_row or {}
    coverage = row.get("fine_trace_coverage") or {}
    reasoning = row.get("reasoning") or {}
    runtime = row.get("runtime") or {}
    return {
        "trace_coverage": {
            "node_recall": (coverage.get("nodes") or {}).get("recall"),
            "edge_recall": (coverage.get("edges") or {}).get("recall"),
            "nodes": coverage.get("nodes"),
            "edges": coverage.get("edges"),
            "stage_coverage": coverage.get("stage_coverage"),
            "unavailable": coverage.get("unavailable"),
        },
        "reasoning": {
            "dimension_scores": reasoning.get("dimension_scores"),
            "unavailable": reasoning.get("unavailable"),
        },
        "reachability": {
            "submitted_unique_pocs": runtime.get("submitted_unique_pocs"),
            "executed_candidates": runtime.get("reachability_executed_candidates"),
            "unavailable": runtime.get("unavailable"),
            "candidates": [
                {
                    "attempt_id": candidate.get("attempt_id"),
                    "execution_status": candidate.get("execution_status"),
                    "target_vulnerability_triggered": candidate.get("target_vulnerability_triggered"),
                    "reachability_depth": (candidate.get("location_reachability") or {}).get("reachability_depth"),
                    "ladder": {
                        key: (candidate.get("location_reachability") or {}).get(key)
                        for key, _stage in _LADDER
                    },
                }
                for candidate in (runtime.get("candidates") or [])
                if isinstance(candidate, dict)
            ],
        },
    }


def failure_mode_key(outcome_record: dict[str, Any], vulnerability_type: str | None) -> str:
    stage = outcome_record.get("first_failed_stage") or outcome_record.get("deepest_stage") or "Unknown"
    return f"{stage}|{(vulnerability_type or 'unknown').strip().lower()}"


def _depth(stage: str | None) -> int:
    return STAGE_ORDER.index(stage) + 1 if stage in STAGE_ORDER else 0
