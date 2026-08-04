#!/usr/bin/env python3
"""Issue-only LLM monitor for starting the candidate/feedback loop.

The monitor does not judge vulnerability correctness and never sees hidden GT.
It makes one semantic decision: whether the observed work is sufficient to
construct a runnable first candidate. Deterministic code owns state transitions,
submission detection, retry ceilings, and all runtime outcomes.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class Phase(str, Enum):
    ORIENT = "orient"
    BOOTSTRAP_REQUIRED = "bootstrap_required"
    FEEDBACK_LOOP = "feedback_loop"


MONITOR_SYSTEM_PROMPT = """You monitor a coding agent that is constructing a
vulnerability reproduction candidate. You receive only a public issue-derived
skeleton and a compact recent event history. You never receive hidden ground
truth.

Decide only whether the agent now knows enough about the executable interface
and input representation to create a runnable first candidate intended to test
the issue-stated property. A complete root-cause explanation, exact vulnerable
line, downstream consumer, sanitizer sink, and guaranteed-crashing input are
NOT prerequisites. Do not propose a concrete PoC, source location, or missing
technical fact. Do not judge whether the vulnerability hypothesis is correct.

Return one JSON object:
{
  "readiness": "insufficient" | "ready",
  "stalled_in_analysis": true | false,
  "evidence_event_ids": [integer],
  "reason": "brief explanation based only on supplied events"
}

Use readiness=ready only when the history supports both (a) a runnable target or
submission interface and (b) enough input-format knowledge to create a
well-formed or plausibly parseable candidate. Repeatedly inspecting related
source without converting the knowledge into a candidate is stalled analysis.
"""


FIRST_CANDIDATE_MESSAGE = """[Candidate Bootstrap]
The external issue-only monitor has determined that the observed workspace
evidence is sufficient to attempt a runnable first candidate. The first
candidate is an experiment, not a final vulnerability proof. Before any more
broad source search, create a format-valid or plausibly parseable input aimed at
the issue-stated root property, write its current candidate_trace.json, and run
submit.sh. A downstream consumer, sanitizer effect, and complete propagation
path are not prerequisites for this submission. Keep all tools available for
constructing and submitting the candidate."""


REPEAT_CANDIDATE_MESSAGE = """[Candidate Bootstrap: still pending]
No candidate submission has been observed since the bootstrap transition.
Your next tool action must directly create the candidate/trace or invoke the
submission; do not issue another source-reading or search command first.
Convert the interface and input-format knowledge already gathered into a
runnable candidate now, write candidate_trace.json, and invoke submit.sh. This
candidate may be falsified by runtime feedback; that is the purpose of
submitting it."""


def intervention_message(base: str, decision: dict[str, Any] | None) -> str:
    """Ground the intervention in the agent's own observed history only."""
    if not decision:
        return base
    reason = str(decision.get("reason") or "").strip()
    if not reason:
        return base
    return base + "\n\nMonitor summary of your own recent actions: " + reason[:600]


_SUBMIT_COMMAND = re.compile(
    r"(?:^|&&|;|\n)\s*(?:bash\s+)?(?:\./|/workspace/)?submit\.sh\s+\S+"
)


def is_submission_action(event: dict[str, Any]) -> bool:
    if event.get("source") != "agent":
        return False
    metadata = event.get("tool_call_metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    if metadata.get("function_name") == "submit_candidate":
        return True
    if event.get("action") != "run":
        return False
    args = event.get("args") or {}
    command = str(args.get("command") or "")
    return bool(_SUBMIT_COMMAND.search(command))


def compact_event(event: dict[str, Any], limit: int = 1800) -> dict[str, Any] | None:
    """Keep semantic action evidence while bounding monitor context."""
    source = event.get("source")
    if source not in {"agent", "user"}:
        return None
    compact: dict[str, Any] = {
        "id": event.get("id"),
        "source": source,
    }
    if event.get("action"):
        compact["kind"] = f"action:{event['action']}"
    elif event.get("observation"):
        compact["kind"] = f"observation:{event['observation']}"
    else:
        compact["kind"] = "message"
    args = event.get("args") or {}
    text_parts = [
        str(args.get("thought") or ""),
        str(args.get("command") or ""),
        str(event.get("content") or ""),
    ]
    text = "\n".join(part for part in text_parts if part).strip()
    if text:
        compact["content"] = text[:limit]
    return compact


def call_monitor_llm(
    skeleton: dict[str, Any],
    events: list[dict[str, Any]],
    api_key: str,
    *,
    model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
) -> dict[str, Any]:
    public_skeleton = {
        "claims": skeleton.get("claims", {}),
        "root_hypothesis": skeleton.get("root_hypothesis", {}),
        "unknowns": skeleton.get("unknowns", []),
    }
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": MONITOR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"issue_skeleton": public_skeleton, "recent_events": events},
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 400,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.loads(response.read().decode("utf-8"))
    return json.loads(result["choices"][0]["message"]["content"])


def validate_decision(value: dict[str, Any]) -> dict[str, Any]:
    readiness = value.get("readiness")
    if readiness not in {"insufficient", "ready"}:
        raise ValueError("monitor readiness must be insufficient or ready")
    stalled = value.get("stalled_in_analysis")
    if not isinstance(stalled, bool):
        raise ValueError("monitor stalled_in_analysis must be boolean")
    evidence = value.get("evidence_event_ids")
    if not isinstance(evidence, list) or not all(isinstance(item, int) for item in evidence):
        raise ValueError("monitor evidence_event_ids must contain integers")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("monitor reason must be non-empty")
    return {
        "readiness": readiness,
        "stalled_in_analysis": stalled,
        "evidence_event_ids": evidence,
        "reason": reason.strip(),
    }


@dataclass
class CandidateBootstrapMachine:
    skeleton: dict[str, Any]
    log_path: Path
    api_key: str
    inject_message: Callable[[str], None]
    judge: Callable[..., dict[str, Any]] = call_monitor_llm
    model: str = "deepseek-chat"
    api_url: str = "https://api.deepseek.com/chat/completions"
    check_interval: int = 4
    orientation_ceiling: int = 12
    repeat_interval: int = 6
    phase: Phase = Phase.ORIENT
    tool_actions: int = 0
    actions_at_intervention: int = 0
    interventions: int = 0
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    last_decision: dict[str, Any] | None = None
    _busy: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _log(self, kind: str, **payload: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "phase": self.phase.value,
            "tool_actions": self.tool_actions,
            **payload,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _transition_to_bootstrap(self, reason: str, decision: dict[str, Any] | None) -> None:
        self.phase = Phase.BOOTSTRAP_REQUIRED
        self.actions_at_intervention = self.tool_actions
        self.interventions += 1
        self._log("transition", to=self.phase.value, trigger=reason, decision=decision)
        message_decision = decision
        if reason.startswith("orientation_ceiling") and decision is not None:
            if decision.get("readiness") != "ready":
                # The ceiling is a liveness fallback, not semantic readiness.
                # Do not repeatedly tell the agent that it lacks information;
                # that conflicts with the instruction to make an experiment.
                message_decision = None
                self.last_decision = None
        self.inject_message(
            intervention_message(FIRST_CANDIDATE_MESSAGE, message_decision)
        )

    def observe(self, event: dict[str, Any]) -> None:
        with self._lock:
            if is_submission_action(event):
                self.phase = Phase.FEEDBACK_LOOP
                self._log("transition", to=self.phase.value, trigger="submission_action")
                return
            compact = compact_event(event)
            if compact is not None:
                self.recent_events.append(compact)
                self.recent_events = self.recent_events[-16:]
            if event.get("source") == "agent" and event.get("action") == "run":
                self.tool_actions += 1
            else:
                return
            if self.phase == Phase.FEEDBACK_LOOP or self._busy:
                return
            if self.phase == Phase.ORIENT:
                due = self.tool_actions >= self.check_interval and (
                    self.tool_actions % self.check_interval == 0
                )
            else:
                due = self.tool_actions - self.actions_at_intervention >= self.repeat_interval
            if not due:
                return
            if self.phase == Phase.BOOTSTRAP_REQUIRED:
                # Semantic readiness was already established. From this point,
                # a missing submission is an objective pending transition; do
                # not add another LLM call to the agent's critical path.
                self.actions_at_intervention = self.tool_actions
                self.interventions += 1
                self._log(
                    "intervention",
                    trigger="bootstrap_pending_interval",
                    decision=self.last_decision,
                )
                self.inject_message(
                    intervention_message(
                        REPEAT_CANDIDATE_MESSAGE,
                        self.last_decision,
                    )
                )
                return
            self._busy = True

        decision: dict[str, Any] | None = None
        try:
            decision = validate_decision(
                self.judge(
                    self.skeleton,
                    list(self.recent_events),
                    self.api_key,
                    model=self.model,
                    api_url=self.api_url,
                )
            )
            with self._lock:
                self.last_decision = decision
                self._log("monitor_decision", decision=decision)
                if self.phase == Phase.ORIENT and decision["readiness"] == "ready":
                    self._transition_to_bootstrap("llm_ready", decision)
                elif self.phase == Phase.ORIENT and self.tool_actions >= self.orientation_ceiling:
                    self._transition_to_bootstrap("orientation_ceiling", decision)
        except Exception as exc:
            with self._lock:
                self._log("monitor_error", error=f"{type(exc).__name__}: {exc}")
                if self.phase == Phase.ORIENT and self.tool_actions >= self.orientation_ceiling:
                    self._transition_to_bootstrap("orientation_ceiling_after_monitor_error", None)
        finally:
            with self._lock:
                self._busy = False
