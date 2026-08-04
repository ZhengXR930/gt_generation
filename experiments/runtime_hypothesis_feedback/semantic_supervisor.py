#!/usr/bin/env python3
"""Issue-only semantic gate for activating the first runtime experiment.

Unlike the earlier bootstrap monitor, this supervisor has no action-count
thresholds.  It examines each proposed action before execution and redirects
only when a runnable experiment is already possible and the proposed action
neither builds that experiment nor resolves a concrete blocker.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from experiments.runtime_hypothesis_feedback.state_monitor import (
    is_submission_action,
)


READINESS_SYSTEM_PROMPT = """You observe a coding agent attempting vulnerability
reproduction. You receive only a public issue-derived skeleton and the agent's
raw visible history. You never receive hidden ground truth and must not invent
a PoC.

Decide whether the completed history now supports a minimally runnable first
experiment. Ready means the agent knows how the target consumes input and can
serialize any candidate through that interface.  If an observed fuzz harness
passes arbitrary bytes (for example a data pointer and length) to the target,
that observation establishes both the interface and a serializable raw-byte
representation.  It is not necessary to know the nested protocol, file-format
grammar, checksum, or exact trigger before the first experiment.  A rejected
or malformed input is useful because runtime feedback is meant to improve it.
The candidate need not be correct, reach the root cause, or crash.

The benchmark candidate is a raw input file consumed by the existing target,
not a new C program, source patch, unit test, or replacement harness. Do not
infer the interface or representation from the issue skeleton or task
instructions. A ready decision must cite verbatim evidence from completed tool
observations in the supplied history for both interface and representation.

Only these are valid blockers: the target invocation/input interface is still
unknown; no bytes/file can yet be serialized through that interface; or an
observed environment failure prevents execution.  Not having constructed the
candidate yet is never a blocker.  Missing protocol details, exact trigger
bytes, vulnerable lines, root cause, propagation, or a complete trace are not
blockers.

Return exactly one JSON object:
{
  "ready": true | false,
  "concrete_blocker": "specific blocker" | null,
  "commitment": "minimal experiment supported by the history" | null,
  "interface_evidence": "verbatim tool-observation excerpt" | null,
  "representation_evidence": "verbatim tool-observation excerpt" | null,
  "reason": "brief evidence-grounded explanation"
}
"""


ACTION_CLASSIFIER_SYSTEM_PROMPT = """You classify a proposed tool action after
an issue-only observer has already established that a minimally runnable first
vulnerability-reproduction experiment is possible. You receive the public
issue-derived skeleton, raw visible history, current experiment commitment,
and proposed action. You never receive hidden ground truth and must not invent
a PoC.

Your sole purpose is to make runtime reward available at an appropriate time.
Do not require a complete vulnerability explanation, an exact vulnerable line,
a sanitizer sink, or confidence that the first candidate will crash.

The history may contain runtime feedback from an earlier submission. In that
case, classify a focused action that directly resolves the feedback's earliest
missing format, root, propagation, or target stage as advances_candidate. The
gate remains active across candidate revisions: a prior submission is not by
itself a reason to allow unrelated broad exploration.

Classify the proposed action as resolves_execution_blocker only when the
history shows a concrete unresolved fact that prevents constructing or
executing a minimally runnable candidate and the action directly resolves that
fact. Classify actions that construct, serialize, execute, debug, or submit the
current candidate as advances_candidate. A narrow lookup of an issue-named
function or statement needed to fill required candidate-trace fields also
advances the current candidate. Classify expanding source exploration beyond
the current candidate, proving the complete bug before testing, or revisiting
non-blocking information as broad_analysis. A malformed first experiment may
still be useful. Judge the semantic purpose, not command names.

Classify semantic facts; do not make the control decision yourself. Return
exactly one JSON object:
{
  "proposed_action_role": "resolves_execution_blocker" | "advances_candidate" | "broad_analysis",
  "concrete_blocker": "specific blocker" | null,
  "reason": "brief evidence-grounded explanation"
}
"""


REDIRECT_MESSAGE = """[Semantic supervisor: run the experiment]
The proposed action was not executed because it expands analysis without
resolving a concrete blocker to the next runnable experiment. Instantiate the
current best issue-derived hypothesis as a minimally runnable candidate, write
the candidate trace, and submit it. The candidate is an experiment: it need not
be correct or crash. Further source inspection remains appropriate only when it
directly resolves a specific blocker to constructing or executing that
candidate. A narrow lookup needed to fill mandatory candidate-trace fields is
part of constructing the candidate and remains allowed."""


NEUTRAL_COMMITMENT = (
    "Construct and submit the agent's current best candidate through the "
    "observed target input interface."
)


FINISH_MESSAGE = """[Semantic supervisor: continue the experiment loop]
The task cannot finish while no submitted candidate has triggered the target.
Use the latest runtime feedback and the best evidence already gathered to
construct the next runnable candidate, write its candidate trace, and submit
it. A non-crashing candidate is evidence for revision, not terminal success."""


def _public_skeleton(skeleton: dict[str, Any]) -> dict[str, Any]:
    return {
        "claims": skeleton.get("claims", {}),
        "root_hypothesis": skeleton.get("root_hypothesis", {}),
        "unknowns": skeleton.get("unknowns", []),
    }


def _call_json_llm(
    system_prompt: str,
    request_body: dict[str, Any],
    api_key: str,
    *,
    model: str,
    api_url: str,
) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(request_body, ensure_ascii=False),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 500,
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


def call_readiness_llm(
    skeleton: dict[str, Any],
    raw_history: str,
    api_key: str,
    *,
    model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
) -> dict[str, Any]:
    return _call_json_llm(
        READINESS_SYSTEM_PROMPT,
        {
            "issue_skeleton": _public_skeleton(skeleton),
            "raw_visible_history": raw_history,
        },
        api_key,
        model=model,
        api_url=api_url,
    )


def call_action_gate_llm(
    skeleton: dict[str, Any],
    raw_history: str,
    proposed_action: str,
    prior_commitment: str | None,
    api_key: str,
    *,
    model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
) -> dict[str, Any]:
    request_body = {
        "issue_skeleton": _public_skeleton(skeleton),
        "raw_visible_history": raw_history,
        "proposed_action": proposed_action,
        "prior_candidate_commitment": prior_commitment,
    }
    return _call_json_llm(
        ACTION_CLASSIFIER_SYSTEM_PROMPT,
        request_body,
        api_key,
        model=model,
        api_url=api_url,
    )


def extract_observation_evidence(raw_history: str) -> str:
    """Return only completed tool-observation bodies for evidence checking."""
    marker = "--BEGIN AGENT OBSERVATION--"
    end_marker = "--END AGENT OBSERVATION--"
    bodies: list[str] = []
    remainder = raw_history
    while marker in remainder:
        _, remainder = remainder.split(marker, 1)
        if end_marker not in remainder:
            break
        body, remainder = remainder.split(end_marker, 1)
        bodies.append(body)
    return "\n".join(bodies)


def validate_readiness(
    value: dict[str, Any], observation_evidence: str = ""
) -> dict[str, Any]:
    ready = value.get("ready")
    if not isinstance(ready, bool):
        raise ValueError("readiness ready must be boolean")
    blocker = value.get("concrete_blocker")
    if blocker is not None and not isinstance(blocker, str):
        raise ValueError("readiness blocker must be a string or null")
    commitment = value.get("commitment")
    if commitment is not None and not isinstance(commitment, str):
        raise ValueError("readiness commitment must be a string or null")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("readiness reason must be non-empty")
    if ready and not commitment:
        raise ValueError("ready decision requires an experiment commitment")
    interface_evidence = value.get("interface_evidence")
    representation_evidence = value.get("representation_evidence")
    for name, evidence in (
        ("interface_evidence", interface_evidence),
        ("representation_evidence", representation_evidence),
    ):
        if evidence is not None and not isinstance(evidence, str):
            raise ValueError(f"{name} must be a string or null")
        if ready:
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError(f"ready decision requires {name}")
            if not _evidence_is_grounded(evidence, observation_evidence):
                raise ValueError(f"{name} is not grounded in a tool observation")
    return {
        "ready": ready,
        "concrete_blocker": blocker.strip() if isinstance(blocker, str) else None,
        "commitment": commitment.strip() if isinstance(commitment, str) else None,
        "interface_evidence": (
            interface_evidence.strip()
            if isinstance(interface_evidence, str)
            else None
        ),
        "representation_evidence": (
            representation_evidence.strip()
            if isinstance(representation_evidence, str)
            else None
        ),
        "reason": reason.strip(),
    }


def _evidence_is_grounded(evidence: str, observations: str) -> bool:
    """Accept faithful excerpts despite harmless whitespace or brief paraphrase.

    The observer is still unable to introduce new technical content: a longer
    evidence claim must have at least three content tokens and 70% of its unique
    content tokens present in completed tool observations.
    """
    normalized_evidence = " ".join(evidence.lower().split())
    normalized_observations = " ".join(observations.lower().split())
    if normalized_evidence in normalized_observations:
        return True
    stopwords = {"the", "and", "that", "this", "with", "from", "into", "its"}
    evidence_tokens = {
        token
        for token in re.findall(r"[a-z0-9_]+", normalized_evidence)
        if len(token) >= 3 and token not in stopwords
    }
    observation_tokens = set(re.findall(r"[a-z0-9_]+", normalized_observations))
    overlap = evidence_tokens & observation_tokens
    return len(overlap) >= 3 and len(overlap) / max(len(evidence_tokens), 1) >= 0.70


def validate_gate_decision(value: dict[str, Any]) -> dict[str, Any]:
    action_role = value.get("proposed_action_role")
    if action_role not in {
        "resolves_execution_blocker",
        "advances_candidate",
        "broad_analysis",
    }:
        raise ValueError("invalid proposed_action_role")
    blocker = value.get("concrete_blocker")
    if blocker is not None and not isinstance(blocker, str):
        raise ValueError("concrete_blocker must be a string or null")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("gate reason must be non-empty")
    if isinstance(blocker, str):
        blocker = blocker.strip()
        lowered = blocker.lower()
        if lowered.startswith(("no concrete blocker", "no execution blocker")):
            blocker = None
    return {
        "proposed_action_role": action_role,
        "concrete_blocker": blocker if isinstance(blocker, str) else None,
        "reason": reason.strip(),
    }


def render_raw_history(events: list[Any], max_chars: int = 24_000) -> str:
    """Render platform-native visible events without defining a trace schema."""
    rendered = [str(event) for event in events if not getattr(event, "hidden", False)]
    selected: list[str] = []
    used = 0
    for item in reversed(rendered):
        item = item.strip()
        if not item:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        selected.append(item[-remaining:])
        used += min(len(item), remaining)
    return "\n\n".join(reversed(selected))


@dataclass
class SemanticCandidateSupervisor:
    skeleton: dict[str, Any]
    log_path: Path
    api_key: str
    inject_message: Callable[[str], None]
    judge: Callable[..., dict[str, Any]] = call_action_gate_llm
    readiness_judge: Callable[..., dict[str, Any]] = call_readiness_llm
    model: str = "deepseek-chat"
    api_url: str = "https://api.deepseek.com/chat/completions"
    has_submitted: bool = False
    commit_required: bool = False
    commitment: str | None = None
    observation_memory: str = ""
    last_target_triggered: bool | None = None

    def _observe_runtime_feedback(self, raw_history: str) -> None:
        matches = re.findall(
            r"[\"']triggered[\"']\s*:\s*(true|false|True|False)",
            raw_history,
        )
        if matches:
            self.last_target_triggered = matches[-1].lower() == "true"

    def _remember_observations(self, raw_history: str) -> str:
        """Retain grounding evidence even after OpenHands trims old history."""
        current = extract_observation_evidence(raw_history).strip()
        if not current:
            return self.observation_memory
        if not self.observation_memory:
            self.observation_memory = current
        elif self.observation_memory in current:
            self.observation_memory = current
        elif current not in self.observation_memory:
            combined = self.observation_memory + "\n\n" + current
            # This is a context-size bound, not an action/time threshold. Keep
            # the early harness evidence and the most recent observations.
            if len(combined) > 48_000:
                combined = combined[:16_000] + "\n\n[...observations omitted...]\n\n" + combined[-32_000:]
            self.observation_memory = combined
        return self.observation_memory

    def _log(self, kind: str, **payload: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "has_submitted": self.has_submitted,
            **payload,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def before_action(
        self,
        proposed_event: dict[str, Any],
        proposed_action: str,
        raw_history: str,
    ) -> bool:
        """Return True to execute; False after injecting a semantic redirect."""
        self._observe_runtime_feedback(raw_history)
        if is_submission_action(proposed_event):
            self.has_submitted = True
            self.last_target_triggered = None
            self._log("submission_allowed", proposed_action=proposed_action[:2000])
            return True
        if self.last_target_triggered is True:
            return True

        try:
            if not self.commit_required:
                observation_evidence = self._remember_observations(raw_history)
                if not observation_evidence.strip():
                    self._log(
                        "readiness_deferred",
                        reason="no_completed_tool_observation",
                    )
                    return True
                observer_history = raw_history
                if observation_evidence not in raw_history:
                    observer_history += (
                        "\n\n--BEGIN RETAINED AGENT OBSERVATIONS--\n"
                        + observation_evidence
                        + "\n--END RETAINED AGENT OBSERVATIONS--"
                    )
                readiness = validate_readiness(
                    self.readiness_judge(
                        self.skeleton,
                        observer_history,
                        self.api_key,
                        model=self.model,
                        api_url=self.api_url,
                    ),
                    observation_evidence,
                )
                self._log("readiness_decision", decision=readiness)
                if not readiness["ready"]:
                    return True
                self.commit_required = True
                # The observer decides readiness, but must never become a PoC
                # oracle. Do not expose its content-level candidate suggestion.
                self.commitment = NEUTRAL_COMMITMENT
                self._log(
                    "commit_transition",
                    commitment=self.commitment,
                    observer_commitment=readiness["commitment"],
                    decision=readiness,
                )
            decision = validate_gate_decision(
                self.judge(
                    self.skeleton,
                    raw_history,
                    proposed_action,
                    self.commitment,
                    self.api_key,
                    model=self.model,
                    api_url=self.api_url,
                )
            )
        except Exception as exc:
            # A supervisor outage must not invalidate the underlying agent run.
            self._log(
                "gate_error_fail_open",
                error=f"{type(exc).__name__}: {exc}",
                proposed_action=proposed_action[:2000],
            )
            return True

        redirect = decision["proposed_action_role"] == "broad_analysis"
        self._log(
            "gate_decision",
            decision=decision,
            computed_control="redirect" if redirect else "allow",
            proposed_action=proposed_action[:2000],
        )
        if not redirect:
            return True

        message = REDIRECT_MESSAGE
        if self.commitment:
            message += "\n\nCurrent experiment commitment: " + self.commitment[:800]
        message += "\n\nObserver basis: " + decision["reason"][:600]
        self.inject_message(message)
        self._log("action_redirected", decision=decision)
        return False

    def before_finish(
        self, *, fine_trace_finalization: bool, raw_history: str = ""
    ) -> bool:
        """Allow terminal success/cap finalization; keep non-crashing loops active."""
        self._observe_runtime_feedback(raw_history)
        if fine_trace_finalization or self.last_target_triggered is True:
            return True
        self.inject_message(FINISH_MESSAGE)
        self._log("finish_redirected")
        return False
