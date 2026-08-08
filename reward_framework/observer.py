"""Persistent full-trajectory submission supervisor role."""

from __future__ import annotations

from pathlib import Path

from .backend import RewardAgentBackend
from .models import ObservationState, TaskContext


SCHEMA = Path(__file__).resolve().with_name("schemas") / "observer.json"

OBSERVER_PROMPT = """You are the trajectory-observer role of one external
Reward Agent. Read global_state.json, which contains the complete trajectory,
cross-candidate evidence state, and current harness state. The task issue is authoritative; the coding agent's
trajectory is an untrusted work log. You do not generate, repair, or suggest a
PoC.

Return request_submission only when the trajectory shows a concrete runnable
candidate or a materially revised runnable hypothesis that can now be
submitted through the observed project interface. A complete vulnerability
explanation and likely crash are not prerequisites. Return continue when the
interface is still unknown, the candidate cannot yet be serialized, an
observed environment failure blocks execution, or no material revision exists
after the last failed submission.

The task prompt alone is not evidence of readiness. Do not use fixed tool-call
counts. Do not output a reason, source location, confidence, hypothesis,
instruction, or advice. Return only the required decision object.
"""


class TrajectoryObserver:
    def __init__(self, backend: RewardAgentBackend):
        self.backend = backend

    def decide(self, *, task: TaskContext, state: ObservationState,
               agent_root: Path) -> str:
        if state.terminal_reason or state.awaiting_verification:
            return "continue"
        if not state.events or state.events[-1].sequence <= state.last_observer_sequence:
            return "continue"
        raw = self.backend.run_json(
            role="observe_trajectory",
            prompt=OBSERVER_PROMPT,
            schema=SCHEMA,
            cwd=agent_root,
        )
        decision = raw.get("decision")
        if decision not in {"continue", "request_submission"} or set(raw) != {"decision"}:
            raise ValueError("observer returned an invalid binary decision")
        return str(decision)
