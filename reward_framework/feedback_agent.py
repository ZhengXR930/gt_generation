"""Evidence-grounded factual feedback with deterministic fallback."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .backend import RewardAgentBackend
from .models import Feedback, RawRuntimeReport, StageAssessment


SCHEMA = Path(__file__).resolve().with_name("schemas") / "feedback.json"

FEEDBACK_PROMPT = """You are the feedback role of one persistent external
Reward Agent. Read task_context.json, observation_state.json,
current_trace.json, current_runtime.json, and prior_evidence.json when present.
The issue is authoritative, the coding-agent trace is untrusted, and the
controller-owned runtime facts and stage assessment are trusted but may be
incomplete.

Explain only what the execution established: the last confirmed stage, the
first evidence boundary, any candidate claim contradicted by trusted runtime or
source facts, and factual change from the previous distinct candidate. Absence
of evidence is unresolved, not refuted. Location reachability alone does not
confirm a vulnerable state. A later-stage location observed behind a failed
gate must not be presented as causal confirmation.

Select only exact IDs from the runtime fact catalog. Do not change stage
statuses. Do not suggest a mutation, byte, field value, command, patch, next
step, alternate consumer, or complete PoC. Do not tell the coding agent what it
should try. Return concise factual English.
"""

_ADVICE = re.compile(
    r"\b(you should|should try|try to|next step|modify|change the|set the|"
    r"craft|construct|use .* bytes?|recommend|suggest)\b",
    re.IGNORECASE,
)


def deterministic_delta(previous: dict[str, Any] | None,
                        assessment: StageAssessment) -> str:
    current = assessment.to_dict()["stages"]
    if previous is None:
        return "This is the first distinct candidate with runtime evidence."
    old = ((previous.get("assessment") or {}).get("stages") or {})
    changed = [f"{stage}: {old.get(stage, 'unknown')} -> {current[stage]}"
               for stage in current if old.get(stage) != current[stage]]
    return "; ".join(changed) if changed else "No stage-status change from the previous distinct candidate."


def fallback_feedback(report: RawRuntimeReport, assessment: StageAssessment,
                      delta: str) -> Feedback:
    facts = tuple(fact.fact_id for fact in report.facts[:4])
    prefix = ", ".join(assessment.longest_confirmed_prefix) or "none"
    boundary = assessment.first_unresolved or "none"
    summary = (
        f"Confirmed causal prefix: {prefix}. First unresolved boundary: {boundary}. "
        f"Trigger observed: {str(report.trigger_observed).lower()}."
    )
    refuted = [stage for stage, status in assessment.stages.items()
               if status.value == "refuted"]
    contradiction = (
        "Trusted runtime evidence refuted the candidate claim at: " + ", ".join(refuted)
        if refuted else None
    )
    return Feedback(summary, contradiction, delta, facts, assessment)


class FeedbackAgent:
    def __init__(self, backend: RewardAgentBackend):
        self.backend = backend

    def generate(self, *, report: RawRuntimeReport, assessment: StageAssessment,
                 previous: dict[str, Any] | None, agent_root: Path) -> Feedback:
        delta = deterministic_delta(previous, assessment)
        try:
            raw = self.backend.run_json(
                role="generate_feedback",
                prompt=FEEDBACK_PROMPT,
                schema=SCHEMA,
                cwd=agent_root,
            )
            allowed = {fact.fact_id for fact in report.facts}
            evidence_ids = tuple(str(value) for value in raw.get("evidence_ids", []))
            if any(value not in allowed for value in evidence_ids):
                raise ValueError("feedback selected an unknown evidence id")
            summary = str(raw.get("summary") or "").strip()
            contradiction_value = raw.get("contradiction")
            contradiction = (
                None if contradiction_value is None
                else str(contradiction_value).strip()
            )
            model_delta = str(raw.get("delta") or "").strip()
            prose = " ".join(x for x in (summary, contradiction or "", model_delta) if x)
            if not summary or _ADVICE.search(prose):
                raise ValueError("feedback is empty or contains advice")
            return Feedback(summary, contradiction, model_delta or delta,
                            evidence_ids, assessment)
        except (RuntimeError, ValueError, KeyError, json.JSONDecodeError):
            return fallback_feedback(report, assessment, delta)
