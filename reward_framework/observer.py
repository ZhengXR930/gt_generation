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

Return request_submission in either of two readiness states. First, the
trajectory shows a concrete runnable candidate or a materially revised
runnable candidate that can be submitted through the observed project
interface. Second, no artifact exists yet, but the Subject has made both the
issue-relevant failure mechanism and the external input/execution interface
concrete enough to construct its own smallest runnable candidate, and is
continuing broader investigation instead of materializing it. The platform
will distinguish these states using workspace artifacts and turn the second
decision into a materialization reminder. A complete vulnerability
explanation and likely crash are not prerequisites.

Treat a candidate as runnable once the Subject can serialize its own current
input hypothesis and pass it through the task-defined submission or execution
interface. It need not be locally proven, likely to trigger, or supported by a
separately rebuilt target harness: failed task-provided runtime validation is
the evidence boundary that enables Reward. Uncertainty about exact triggering
bytes is not by itself a reason to continue when the input carrier, relevant
structure, and executable interface are already concrete.

In particular, request_submission when the Subject has named the concrete
external carrier or request type, the issue-relevant fields or operations it
intends to encode, and the task-provided execution/submission interface, but is
still reading additional implementation details instead of attempting that
encoding. Do not require a complete byte-offset map, a locally rebuilt target,
or certainty about every intermediate transition. Those are refinements that
runtime rejection is meant to test. Continue only if at least one of the
carrier, issue-relevant structure, or executable interface is genuinely still
unknown rather than merely unfinished.

Return continue when the attacker-controlled input relation, external
interface, or candidate hypothesis is still materially unknown; when an
observed environment failure makes the interface unavailable; or when no
material progress has occurred since the last reminder or failed submission.
If concrete candidate contents and an interface are already specified but the
file-creation/tool call was malformed or did not execute, return
request_submission: materialization is then a harness action boundary, not
missing vulnerability reasoning.

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
        # The public task and injected reminders are never readiness evidence by
        # themselves. Require at least one substantive Subject action before
        # asking the semantic observer to judge candidate maturity.
        if not any(
            event.source.lower() in {"agent", "assistant", "coding_agent"}
            and event.kind not in {
                "ActionType.MESSAGE", "ActionType.RECALL",
                "ObservationType.RECALL",
            }
            for event in state.events
        ):
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
