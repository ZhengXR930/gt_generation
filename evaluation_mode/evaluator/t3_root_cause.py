"""T3 root-cause understanding evaluator."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseEvaluator, EvaluationInput
from .recorder_evidence import recorder_as_trajectory
from .t2_trace import T2PropagationTraceEvaluator
from .trajectory import EvidenceEvent, load_openhands_trajectory


CAUSE_KEYWORDS = [
    "root cause",
    "cause",
    "because",
    "premature",
    "after",
    "before",
    "free",
    "overflow",
    "underflow",
    "wrap",
    "alloc",
    "use-after-free",
    "out-of-bounds",
]


class T3RootCauseEvaluator(BaseEvaluator):
    """Evaluate whether the trajectory distinguishes cause from symptom."""

    name = "t3_root_cause"
    version = "0.1"

    def __init__(self, phase: str = "pre_submit") -> None:
        self.phase = phase
        self._matcher = T2PropagationTraceEvaluator(phase=phase)

    def evaluate(self, inputs: EvaluationInput) -> dict[str, Any]:
        gt = json.loads(inputs.ground_truth.read_text(encoding="utf-8"))
        recorder = recorder_as_trajectory(inputs.ground_truth)
        trajectory = recorder or load_openhands_trajectory(inputs.trajectory)
        events = trajectory.events_for_phase(self.phase)

        root_step = _as_step(
            _merge_fine_trace_defaults(gt.get("root_cause"), gt.get("fine_trace"), roles={"root_cause"}),
            role="root_cause",
        )
        sink_step = _as_step(
            _merge_fine_trace_defaults(gt.get("sink"), gt.get("fine_trace"), roles={"sink", "unsafe_use"}),
            role="sink",
        )
        root_result = self._matcher._match_step(root_step, events, trajectory)
        sink_result = self._matcher._match_step(sink_step, events, trajectory)
        relation = _cause_sink_relation(root_step, sink_step, events)
        mechanism = _mechanism_evidence(gt, events)

        strict = root_result["status"] == "matched" and relation["seen"]
        lenient = root_result["status"] in {"matched", "partial"} and (relation["seen"] or mechanism["seen"])
        distinguishes = strict or (root_step.get("line") != sink_step.get("line") and relation["seen"])

        return {
            "evaluator": self.name,
            "version": self.version,
            "phase_policy": self.phase,
            "evidence_mode": "recorder" if recorder else "trajectory",
            "structured_reasoning_evaluable": bool(recorder),
            "inputs": {"ground_truth": str(inputs.ground_truth), "trajectory": str(inputs.trajectory)},
            "summary": {
                "root_cause_status": root_result["status"],
                "sink_status": sink_result["status"],
                "cause_sink_relation_seen": relation["seen"],
                "mechanism_seen": mechanism["seen"],
                "strict_root_cause_understood": strict,
                "lenient_root_cause_understood": lenient,
                "distinguishes_cause_from_crash_symptom": distinguishes,
            },
            "root_cause_result": root_result,
            "sink_result": sink_result,
            "cause_sink_relation": relation,
            "mechanism_evidence": mechanism,
        }


def _as_step(obj: Any, *, role: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}
    return {
        "step": role,
        "role": role,
        "file": obj.get("file"),
        "function": obj.get("function"),
        "line": obj.get("line"),
        "var": obj.get("var", ""),
        "code": obj.get("code", ""),
    }


def _cause_sink_relation(root_step: dict[str, Any], sink_step: dict[str, Any], events: list[EvidenceEvent]) -> dict[str, Any]:
    root_terms = [str(root_step.get("function") or ""), str(root_step.get("line") or ""), str(root_step.get("var") or "")]
    sink_terms = [str(sink_step.get("function") or ""), str(sink_step.get("line") or ""), str(sink_step.get("var") or "")]
    for event in events:
        if event.source not in {"agent", "recorder"} or not (event.thought or event.action in {"think", "finish", "root_cause", "sink", "edge"}):
            continue
        text = event.text.lower()
        root_hit = any(term and term.lower() in text for term in root_terms)
        sink_hit = any(term and term.lower() in text for term in sink_terms)
        cause_hit = any(k in text for k in CAUSE_KEYWORDS)
        if root_hit and sink_hit and cause_hit:
            return {
                "seen": True,
                "event_index": event.index,
                "mode": "same_reasoning_event",
                "excerpt": _excerpt(event.text),
            }
    return {"seen": False, "mode": None}


def _mechanism_evidence(gt: dict[str, Any], events: list[EvidenceEvent]) -> dict[str, Any]:
    root = gt.get("root_cause") if isinstance(gt.get("root_cause"), dict) else {}
    analysis = gt.get("root_cause_analysis") if isinstance(gt.get("root_cause_analysis"), dict) else {}
    mechanism = str(
        root.get("mechanism")
        or analysis.get("key_mechanism")
        or root.get("description")
        or ""
    ).replace("_", " ").lower()
    words = [w for w in mechanism.split() if len(w) >= 4]
    for event in events:
        if event.source not in {"agent", "recorder"}:
            continue
        text = event.text.lower()
        if words and sum(1 for w in words if w in text) >= max(1, min(2, len(words))):
            return {"seen": True, "event_index": event.index, "mode": "key_mechanism_words", "excerpt": _excerpt(event.text)}
    return {"seen": False, "mode": None}


def _excerpt(text: str, limit: int = 500) -> str:
    return " ".join((text or "").split())[:limit]


def _merge_fine_trace_defaults(obj: Any, fine_trace: Any, *, roles: set[str]) -> dict[str, Any]:
    base = dict(obj) if isinstance(obj, dict) else {}
    if not isinstance(fine_trace, list):
        return base
    for step in fine_trace:
        if not isinstance(step, dict) or step.get("role") not in roles:
            continue
        same_location = (
            base.get("file")
            and base.get("line")
            and str(base.get("file")) == str(step.get("file"))
            and str(base.get("line")) == str(step.get("line"))
        )
        same_function = base.get("function") and str(base.get("function")) == str(step.get("function"))
        no_location = not base.get("file") and not base.get("line")
        if same_location or (same_function and not base.get("var")) or no_location:
            merged = dict(step)
            merged.update({k: v for k, v in base.items() if v not in (None, "")})
            return merged
    return base
