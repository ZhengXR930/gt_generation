"""Evidence-grounded factual feedback with deterministic fallback."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .backend import RewardAgentBackend
from .models import Feedback, RawRuntimeReport, StageAssessment
from .assertion_reward import AssertionAssessment


SCHEMA = Path(__file__).resolve().with_name("schemas") / "feedback.json"

FEEDBACK_PROMPT = """You are the feedback role of one persistent external
Reward Agent. The controller supplies exact JSON snapshots below.
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


def _state_snapshot(agent_root: Path) -> str:
    sections = []
    for name in (
        "global_state.json", "current_trace.json", "current_runtime.json",
        "prior_evidence.json",
    ):
        path = agent_root / name
        if not path.is_file():
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        sections.append(
            f"\n{name}:\n" + json.dumps(value, ensure_ascii=False, sort_keys=True)
        )
    return "\nCONTROLLER-SUPPLIED STATE (verbatim):\n" + "".join(sections)

_ADVICE = re.compile(
    r"\b(you should|should try|try to|next step|modify|change the|set the|"
    r"craft|construct|use .* bytes?|recommend|suggest)\b",
    re.IGNORECASE,
)


def deterministic_delta(previous: dict[str, Any] | None,
                        assessment: StageAssessment | AssertionAssessment) -> str:
    current_value = assessment.to_dict()
    if current_value.get("protocol") == "assertion-reward-v1":
        old_claims = {
            str(item.get("assertion_id")): item
            for item in ((previous or {}).get("assessment") or {}).get("claims", [])
            if isinstance(item, dict)
        }
        changed = []
        for item in current_value.get("claims", []):
            old = old_claims.get(str(item.get("assertion_id")))
            if old is None or old.get("status") != item.get("status"):
                changed.append(
                    f"{item.get('assertion_id')}: "
                    f"{(old or {}).get('status', 'not_evaluated')} -> {item.get('status')}"
                )
        return "; ".join(changed) if changed else "No assertion-status change from the previous distinct candidate."
    current = assessment.to_dict()["stages"]
    if previous is None:
        return "This is the first distinct candidate with runtime evidence."
    old = ((previous.get("assessment") or {}).get("stages") or {})
    changed = [f"{stage}: {old.get(stage, 'unknown')} -> {current[stage]}"
               for stage in current if old.get(stage) != current[stage]]
    return "; ".join(changed) if changed else "No stage-status change from the previous distinct candidate."


def fallback_feedback(report: RawRuntimeReport,
                      assessment: StageAssessment | AssertionAssessment,
                      delta: str) -> Feedback:
    facts = tuple(fact.fact_id for fact in report.facts[:4])
    if isinstance(assessment, AssertionAssessment):
        evaluated = [item for item in assessment.claims if item.evaluated]
        statuses = "; ".join(
            f"{item.assertion_id}={item.status}" for item in assessment.claims
        ) or "none"
        summary = (
            f"Input admission: {assessment.admission}. Runtime assertions: {statuses}. "
            f"New deterministic evidence units: {assessment.information_gain}. "
            f"Trigger observed: {str(report.trigger_observed).lower()}."
        )
        contradiction = (
            "The trigger oracle conflicts with the currently executable assertion Spec; "
            "claim-directed feedback is withheld."
            if assessment.consistency == "spec_or_mapping_conflict" else None
        )
        return Feedback(summary, contradiction, delta, facts, assessment)
    prefix = ", ".join(assessment.longest_confirmed_prefix) or "none"
    boundary = assessment.first_unresolved or "none"
    summary = (
        f"Confirmed causal prefix: {prefix}. First unresolved boundary: {boundary}. "
        f"Trigger observed: {str(report.trigger_observed).lower()}."
    )
    if report.trigger_observed and assessment.first_unresolved:
        summary += (
            " The independent trigger is confirmed, while exact stage attribution "
            "remains conservative where passive probe evidence is incomplete."
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

    def generate(self, *, report: RawRuntimeReport,
                 assessment: StageAssessment | AssertionAssessment,
                 previous: dict[str, Any] | None, agent_root: Path) -> Feedback:
        delta = deterministic_delta(previous, assessment)
        # Claim truth values and their polarity are controller-owned and fully
        # deterministic.  Do not ask an LLM to reinterpret them.
        if isinstance(assessment, AssertionAssessment):
            return fallback_feedback(report, assessment, delta)
        try:
            raw = self.backend.run_json(
                role="generate_feedback",
                prompt=FEEDBACK_PROMPT + _state_snapshot(agent_root),
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
            if report.trigger_observed and assessment.first_unresolved:
                summary = (
                    "The independent runtime oracle confirmed the vulnerability trigger. "
                    "Exact causal-stage attribution remains conservative at the unresolved "
                    f"{assessment.first_unresolved} boundary. " + summary
                )
            return Feedback(summary, contradiction, model_delta or delta,
                            evidence_ids, assessment)
        except (RuntimeError, ValueError, KeyError, OSError, json.JSONDecodeError):
            return fallback_feedback(report, assessment, delta)
