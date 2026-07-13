"""Deterministic evaluators for vulnerability reasoning trajectories.

All reasoning evaluators score the agent's structured recorder node-trace:
  * T1SourceSinkEvaluator — localization of the source/sink anchors (endpoints).
  * InvariantEvaluator     — the reasoning BETWEEN the anchors (invariant
                             checkpoints + typed data/control/order edges).
  * T3RootCauseEvaluator   — the cause-vs-symptom distinction and causal link.
  * T4PocSuccessEvaluator  — dynamic PoC-trigger success.

The fuzzy T2 propagation-trace and T5 keyword-rationale evaluators were removed:
propagation is scored structurally by InvariantEvaluator, and rationale by the
cause->crash link in T3RootCauseEvaluator.
"""

from .invariant import InvariantEvaluator
from .t1_source_sink import T1SourceSinkEvaluator
from .t3_root_cause import T3RootCauseEvaluator
from .t4_poc_success import T4PocSuccessEvaluator

__all__ = [
    "T1SourceSinkEvaluator",
    "InvariantEvaluator",
    "T3RootCauseEvaluator",
    "T4PocSuccessEvaluator",
]
