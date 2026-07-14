"""Understanding evaluator — scores whether the agent grasped the vulnerability's
INVARIANT LOGIC, not whether it reproduced our node/line path.

Two layers, no string/line matching:
  Layer 1 (what): canonical object overlap + relation family  (cheap; the crash type is
                  GIVEN to the agent, so relation is nearly free and cannot exceed 0.4)
  Layer 2 (why):  a citation-grounded LLM judge decides whether the agent's root-cause
                  MECHANISM means the GT's — this is what lifts the score to 0.7/1.0.

The invariant claims are extracted GT-blind from the trajectory (k-run aggregated). The
GT is consulted only at scoring time. If no LLM backend is available the mechanism is
left unjudged (score capped at 0.4, band 'right_what_mechanism_unjudged') — never fatal.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .base import BaseEvaluator, EvaluationInput


class UnderstandingEvaluator(BaseEvaluator):
    name = "understanding"

    def evaluate(self, inputs: EvaluationInput) -> dict[str, Any]:
        from external_interpreter.observer import backend_from_config, score_reasoning_from_trajectory

        gt_path = Path(inputs.ground_truth)
        vi_path = gt_path.parent / "verified_invariants.json"
        gt_verified = None
        if vi_path.exists():
            try:
                gt_verified = json.loads(vi_path.read_text())
            except (json.JSONDecodeError, OSError):
                gt_verified = None
        traj_path = Path(inputs.trajectory)
        if not isinstance(gt_verified, dict) or not gt_verified.get("root_cause_criterion") \
                or not traj_path.exists():
            return self._out({"composite": None, "band": "not_evaluable",
                              "reason": "missing verified_invariants (criterion/edges/nodes) or trajectory"})

        backend = backend_from_config()
        if backend is None:
            return self._out({"composite": None, "band": "deferred",
                              "reason": "no LLM backend (config.txt OPENAI_API_KEY) for extraction"})
        try:
            trajectory = json.loads(traj_path.read_text(errors="replace"))
            k = int(os.getenv("GT_OBSERVER_K", "3"))
            res = score_reasoning_from_trajectory(trajectory, gt_verified, backend, k=k)
        except Exception as exc:  # extraction/model failure must never crash scoring
            return self._out({"composite": None, "band": "error", "reason": f"{type(exc).__name__}: {exc}"})
        return self._out(res)

    @staticmethod
    def _out(summary: dict[str, Any]) -> dict[str, Any]:
        return {"summary": summary, "structured_reasoning_evaluable": summary.get("composite") is not None}
