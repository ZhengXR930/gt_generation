"""T1 source-sink identification evaluator."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseEvaluator, EvaluationInput
from .recorder_evidence import recorder_as_trajectory
from .t2_trace import T2PropagationTraceEvaluator
from .trajectory import load_openhands_trajectory


class T1SourceSinkEvaluator(BaseEvaluator):
    """Evaluate whether trajectory evidence identifies source and sink."""

    name = "t1_source_sink"
    version = "0.1"

    def __init__(self, phase: str = "pre_submit") -> None:
        self.phase = phase
        self._matcher = T2PropagationTraceEvaluator(phase=phase)

    def evaluate(self, inputs: EvaluationInput) -> dict[str, Any]:
        gt = json.loads(inputs.ground_truth.read_text(encoding="utf-8"))
        recorder = recorder_as_trajectory(inputs.ground_truth)
        trajectory = recorder or load_openhands_trajectory(inputs.trajectory)
        events = trajectory.events_for_phase(self.phase)

        source = _as_step(
            _merge_fine_trace_defaults(gt.get("source"), gt.get("fine_trace"), roles={"source", "tainted_read"}),
            role="source",
            step_name="source",
        )
        sink = _as_step(
            _merge_fine_trace_defaults(gt.get("sink"), gt.get("fine_trace"), roles={"sink", "unsafe_use"}),
            role="sink",
            step_name="sink",
        )
        source_result = self._matcher._match_step(source, events, trajectory)
        sink_result = self._matcher._match_step(sink, events, trajectory)
        source_value = _as_step(
            _merge_fine_trace_defaults(gt.get("tainted_value_origin"), gt.get("fine_trace"), roles={"tainted_read", "source"}),
            role="tainted_value_origin",
            step_name="tainted_value_origin",
        )
        source_value_result = self._matcher._match_step(source_value, events, trajectory) if source_value.get("file") else None

        source_status = _combine_source_status(source_result["status"], source_value_result["status"] if source_value_result else None)
        sink_status = sink_result["status"]
        strict = source_status == "matched" and sink_status == "matched"
        lenient = source_status in {"matched", "partial"} and sink_status in {"matched", "partial"}

        return {
            "evaluator": self.name,
            "version": self.version,
            "phase_policy": self.phase,
            "evidence_mode": "recorder" if recorder else "trajectory",
            "structured_reasoning_evaluable": bool(recorder),
            "inputs": {"ground_truth": str(inputs.ground_truth), "trajectory": str(inputs.trajectory)},
            "summary": {
                "source_status": source_status,
                "sink_status": sink_status,
                "strict_source_sink_identified": strict,
                "lenient_source_sink_identified": lenient,
            },
            "source_result": source_result,
            "tainted_value_origin_result": source_value_result,
            "sink_result": sink_result,
            "notes": [
                "T1 is scored on source/sink identification only; propagation edges belong to T2.",
                "For parser/fuzzer cases, tainted_value_origin can provide source-side evidence when GT distinguishes harness boundary from parser load point.",
            ],
        }


def _as_step(obj: Any, *, role: str, step_name: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}
    return {
        "step": step_name,
        "role": role,
        "file": obj.get("file"),
        "function": obj.get("function"),
        "line": obj.get("line"),
        "var": obj.get("var") or _var_from_description(obj.get("description", "")),
        "code": obj.get("code", ""),
    }


def _var_from_description(text: str) -> str:
    # Keep this intentionally conservative. Free text descriptions should not
    # invent source variables; they only provide a weak fallback for matching.
    return ""


def _combine_source_status(source_status: str, value_status: str | None) -> str:
    order = {"missing": 0, "weak": 1, "partial": 2, "matched": 3}
    best = source_status
    if value_status and order.get(value_status, 0) > order.get(best, 0):
        best = value_status
    return best


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
