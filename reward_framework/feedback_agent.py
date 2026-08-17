"""Deterministic factual feedback from runtime evidence.

Feedback is intentionally non-prescriptive: it reports observed stage progress,
the first unresolved boundary, contradictions, and change from the previous
distinct candidate.  It does not suggest bytes, fields, commands, patches, or a
next PoC construction strategy.
"""

from __future__ import annotations

from typing import Any

from .models import Feedback, RawRuntimeReport, StageAssessment


def deterministic_delta(previous: dict[str, Any] | None,
                        assessment: StageAssessment) -> str:
    current = assessment.to_dict()["stages"]
    if previous is None:
        return "This is the first distinct candidate with runtime evidence."
    old = ((previous.get("assessment") or {}).get("stages") or {})
    changed = [
        f"{stage}: {old.get(stage, 'unknown')} -> {current[stage]}"
        for stage in current if old.get(stage) != current[stage]
    ]
    return (
        "; ".join(changed)
        if changed else
        "No stage-status change from the previous distinct candidate."
    )


class FeedbackAgent:
    def generate(self, *, report: RawRuntimeReport,
                 assessment: StageAssessment,
                 previous: dict[str, Any] | None) -> Feedback:
        delta = deterministic_delta(previous, assessment)
        prefix = ", ".join(assessment.longest_confirmed_prefix) or "none"
        boundary = assessment.first_unresolved or "none"
        summary = (
            f"Confirmed stage prefix: {prefix}. "
            f"First unresolved boundary: {boundary}. "
            f"Trigger observed: {str(report.trigger_observed).lower()}."
        )
        if assessment.consistency == "spec_or_mapping_conflict":
            summary += (
                " The independent runtime oracle observed a vulnerability signal, "
                "but the stage probes conflict with that oracle at: "
                + ", ".join(assessment.conflict_stages)
                + ". Treat the stage attribution as unreliable for this candidate."
            )
        elif report.trigger_observed and assessment.first_unresolved:
            summary += (
                " The independent runtime oracle observed a vulnerability signal, "
                "but exact stage attribution remains conservative at the unresolved "
                f"{assessment.first_unresolved} boundary."
            )
        refuted = [
            stage for stage, status in assessment.stages.items()
            if status.value == "refuted"
        ]
        contradiction = (
            "Trusted runtime evidence refuted the candidate claim at: "
            + ", ".join(refuted)
            if refuted and assessment.consistency == "consistent" else None
        )
        facts = tuple(fact.fact_id for fact in report.facts[:6])
        return Feedback(summary, contradiction, delta, facts, assessment)
