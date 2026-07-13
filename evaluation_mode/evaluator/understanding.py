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
        from external_interpreter.observer import (
            backend_from_config, score_understanding_from_trajectory,
        )

        gt_path = Path(inputs.ground_truth)
        vi_path = gt_path.parent / "verified_invariants.json"
        criterion = None
        if vi_path.exists():
            try:
                criterion = json.loads(vi_path.read_text()).get("root_cause_criterion")
            except (json.JSONDecodeError, OSError):
                criterion = None
        traj_path = Path(inputs.trajectory)
        if not isinstance(criterion, dict) or not traj_path.exists():
            return self._out({"score": None, "band": "not_evaluable",
                              "reason": "missing verified_invariants.root_cause_criterion or trajectory"})

        # pre-extracted claims (from a prior observer run) take precedence over re-extracting
        pre = traj_path.parent / "observer_claims.json"
        backend = backend_from_config()
        try:
            if pre.exists():
                from external_interpreter.observer import understanding_score
                claims = json.loads(pre.read_text()).get("claims") or []
                res = understanding_score(claims, criterion, backend)
                res.update(k_runs=0, n_claims=len(claims))
            else:
                if backend is None:
                    return self._out({"score": None, "band": "deferred",
                                      "reason": "no LLM backend (config.txt OPENAI_API_KEY) for extraction"})
                trajectory = json.loads(traj_path.read_text(errors="replace"))
                k = int(os.getenv("GT_OBSERVER_K", "3"))
                res = score_understanding_from_trajectory(trajectory, criterion, backend, k=k)
        except Exception as exc:  # backend/model failure must never crash scoring
            return self._out({"score": None, "band": "error", "reason": f"{type(exc).__name__}: {exc}"})
        return self._out(res)

    @staticmethod
    def _out(summary: dict[str, Any]) -> dict[str, Any]:
        return {"summary": summary, "structured_reasoning_evaluable": summary.get("score") is not None}
