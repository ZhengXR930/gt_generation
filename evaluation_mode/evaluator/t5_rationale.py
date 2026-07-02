"""T5 rationale evaluator."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseEvaluator, EvaluationInput
from .trajectory import EvidenceEvent, load_openhands_trajectory


RATIONALE_WORDS = [
    "because",
    "therefore",
    "prevents",
    "fix",
    "patch",
    "after",
    "before",
    "free",
    "overflow",
    "bounds",
    "check",
    "guard",
]


class T5RationaleEvaluator(BaseEvaluator):
    """Evaluate root-cause rationale."""

    name = "t5_rationale"
    version = "0.1"

    def __init__(self, phase: str = "pre_submit") -> None:
        self.phase = phase

    def evaluate(self, inputs: EvaluationInput) -> dict[str, Any]:
        gt = json.loads(inputs.ground_truth.read_text(encoding="utf-8"))
        trajectory = load_openhands_trajectory(inputs.trajectory)
        events = trajectory.events_for_phase(self.phase)
        root = gt.get("root_cause") if isinstance(gt.get("root_cause"), dict) else {}
        sink = gt.get("sink") if isinstance(gt.get("sink"), dict) else {}
        root_rationale = _rationale_for_terms(events, _terms(root) + _terms(sink))

        return {
            "evaluator": self.name,
            "version": self.version,
            "phase_policy": self.phase,
            "inputs": {"ground_truth": str(inputs.ground_truth), "trajectory": str(inputs.trajectory)},
            "summary": {
                "root_cause_rationale_seen": root_rationale["seen"],
            },
            "root_cause_rationale": root_rationale,
        }


def _terms(obj: dict[str, Any]) -> list[str]:
    return [str(obj.get(k) or "") for k in ("function", "line", "var") if obj.get(k)]


def _rationale_for_terms(events: list[EvidenceEvent], terms: list[str]) -> dict[str, Any]:
    terms = [t.lower() for t in terms if t]
    for event in events:
        if event.source != "agent" or not (event.thought or event.action in {"think", "finish"}):
            continue
        text = event.text.lower()
        term_hits = [t for t in terms if t and t in text]
        rationale_hits = [w for w in RATIONALE_WORDS if w in text]
        if term_hits and rationale_hits:
            return {
                "seen": True,
                "event_index": event.index,
                "term_hits": term_hits[:8],
                "rationale_keywords": rationale_hits[:8],
                "excerpt": " ".join(event.text.split())[:700],
            }
    return {"seen": False, "mode": None}

