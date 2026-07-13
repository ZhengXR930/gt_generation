"""Endpoints evaluator: structured localization of the two artifact-grounded
anchors — the source (attacker input load point) and the sink (crash point).

These are deliberately scored separately from the between-source-and-sink reasoning
(handled by the invariant evaluator): the anchors are directly artifact-grounded
(sink = sanitizer crash stack, source = input load) and answer a localization
question, while the invariant evaluator scores the harder propagation reasoning that
connects them. Deterministic; the agent's structured recorder claims are the input.
"""

from __future__ import annotations

import json
from typing import Any

from .base import BaseEvaluator, EvaluationInput
from .invariant import _match_position, build_agent_state, nodes_in_group


class T1SourceSinkEvaluator(BaseEvaluator):
    """Evaluate whether the agent's recorded node-trace locates the source and sink."""

    name = "t1_source_sink"
    version = "0.3"

    def evaluate(self, inputs: EvaluationInput) -> dict[str, Any]:
        gt = json.loads(inputs.ground_truth.read_text(encoding="utf-8"))
        agent, has_records = build_agent_state(inputs.ground_truth)
        source_nodes = nodes_in_group(agent["nodes"], "sources")
        sink_nodes = nodes_in_group(agent["nodes"], "sinks")

        source_pos = _best_source_position(gt, source_nodes)
        sink_pos = _match_position(_loc(gt.get("sink")), sink_nodes)

        source_status = source_pos["status"]
        sink_status = sink_pos["status"]
        strict = source_status == "located" and sink_status == "located"
        lenient = source_status in {"located", "wrong_location"} and sink_status in {"located", "wrong_location"}

        return {
            "evaluator": self.name,
            "version": self.version,
            "structured_reasoning_evaluable": has_records,
            "inputs": {"ground_truth": str(inputs.ground_truth)},
            "summary": {
                "source_status": source_status,
                "sink_status": sink_status,
                "strict_source_sink_identified": strict,
                "lenient_source_sink_identified": lenient,
            },
            "source_result": source_pos,
            "sink_result": sink_pos,
            "notes": [
                "Endpoints are scored on source/sink localization only; the reasoning between "
                "them (intermediate invariants + typed edges) is scored by the invariant evaluator.",
                "The source anchor may be matched either by the GT `source` location or by "
                "`tainted_value_origin` when the GT distinguishes the harness boundary from the "
                "parser load point.",
            ],
        }


def _best_source_position(gt: dict, candidates: list[dict]) -> dict[str, Any]:
    """Match the source anchor against the GT source, falling back to
    tainted_value_origin when it locates a stronger source-side point."""
    best = _match_position(_loc(gt.get("source")), candidates)
    origin = gt.get("tainted_value_origin")
    if best["status"] != "located" and isinstance(origin, dict) and origin.get("file"):
        alt = _match_position(_loc(origin), candidates)
        if _rank(alt["status"]) > _rank(best["status"]):
            best = alt
    return best


def _loc(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        return {}
    return {"file": obj.get("file"), "function": obj.get("function"), "line": obj.get("line")}


def _rank(status: str) -> int:
    return {"missing": 0, "wrong_location": 1, "located": 2}.get(status, 0)
