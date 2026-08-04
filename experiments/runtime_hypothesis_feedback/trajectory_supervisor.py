#!/usr/bin/env python3
"""Deterministic submission state machine driven by one Reward Agent."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from experiments.runtime_hypothesis_feedback.reward_agent import (
    decide_submission,
    public_reward_context,
    validate_readiness_decision,
)
from experiments.runtime_hypothesis_feedback.state_monitor import is_submission_action
from experiments.runtime_hypothesis_feedback.submit_candidate_tool import (
    TOOL_NAME,
    submission_response_outcome,
)


SUBMIT_MESSAGE = """[External trajectory observer: submit]
The task-level Reward Agent has marked the episode SUBMISSION_REQUIRED. Turn
the current hypothesis into an experiment now: write the candidate under
`/workspace/poc` (an extension is allowed), write
`/workspace/candidate_trace.json`, and call `submit_candidate`. The candidate
need not be correct or crash. Further source exploration is paused until this
candidate is submitted; artifact construction tools remain available."""


CONTINUE_AFTER_FINISH_MESSAGE = """[External trajectory observer: continue]
The target has not been triggered, so this is not a successful terminal state.
Continue constructing or revising a runnable PoC using the visible issue and
runtime feedback. Submit when the external observer requests it."""


SUBMIT_PENDING_MESSAGE = """[External trajectory observer: submission still required]
The pending experiment has not been submitted. Only materialize or update the
candidate PoC and candidate_trace.json, then call submit_candidate. Open-ended
source exploration resumes after the runtime result is returned."""


class EpisodeState(str, Enum):
    EXPLORING = "exploring"
    SUBMISSION_REQUIRED = "submission_required"
    VERIFYING = "verifying"
    REVISING = "revising"
    SUCCEEDED = "succeeded"
    EXHAUSTED = "exhausted"


_CANDIDATE_ARTIFACT = re.compile(
    r"/workspace/(?:poc(?:[._A-Za-z0-9-]*)?|candidate_trace\.json|trace\.json)"
)
_SHELL_WRITE = re.compile(
    r"(?:>>?|\btee\b|\bcp\b|\bmv\b|\binstall\b|\btouch\b|"
    r"\bdd\b[^\n]*(?:\bof=)|\bxxd\b[^\n]*\s-r\b)"
)
_PYTHON_WRITE = re.compile(
    r"(?:open\s*\([^\n]*(?:['\"](?:w|a|x)b?['\"])|"
    r"\.write_(?:text|bytes)\s*\(|\.write\s*\()"
)


def is_candidate_artifact_action(event: dict[str, Any]) -> bool:
    """Recognize an action scoped to the candidate artifacts themselves."""
    if event.get("source") != "agent":
        return False
    return bool(_CANDIDATE_ARTIFACT.search(json.dumps(event, ensure_ascii=False)))


def is_candidate_materialization_action(event: dict[str, Any]) -> bool:
    """Recognize an actual candidate write, not a read-only candidate check."""
    if not is_candidate_artifact_action(event):
        return False
    # Platform adapters do not agree on whether a malformed function call is
    # represented as action args, message content, or a provider-native field.
    # Inspect the complete visible proposed event without interpreting its PoC.
    serialized = json.dumps(event, ensure_ascii=False)
    if event.get("action") != "run":
        # Provider-malformed tool markup is surfaced by OpenHands as a plain
        # MessageAction. It describes an intention but performs no filesystem
        # mutation, so it must never advance the episode state.
        return False
    args = event.get("args") or {}
    command = str(args.get("command") or "")
    return bool(_SHELL_WRITE.search(command) or _PYTHON_WRITE.search(command))


def _public_issue_evidence(skeleton: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible view of the unified Reward Agent public context."""
    context = public_reward_context(skeleton)
    return {
        "verbatim_claim_evidence": context["verbatim_issue_evidence"],
        "unknowns": context["unknowns"],
    }


def call_trajectory_observer(
    skeleton: dict[str, Any],
    raw_trajectory: str,
    api_key: str,
    *,
    reward_spec: dict[str, Any] | None = None,
    model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
) -> dict[str, Any]:
    return decide_submission(
        skeleton=skeleton,
        reward_spec=reward_spec,
        raw_trajectory=raw_trajectory,
        api_key=api_key,
        model=model,
        api_url=api_url,
    )


def validate_binary_decision(value: dict[str, Any]) -> str:
    return validate_readiness_decision(value)


def render_visible_trajectory(events: list[Any], max_chars: int = 32_000) -> str:
    """Render platform-native visible events without a semantic trace schema."""
    rendered = [str(event).strip() for event in events if not getattr(event, "hidden", False)]
    rendered = [item for item in rendered if item]
    selected: list[str] = []
    used = 0
    for item in reversed(rendered):
        remaining = max_chars - used
        if remaining <= 0:
            break
        selected.append(item[-remaining:])
        used += min(len(item), remaining)
    return "\n\n".join(reversed(selected))


def _event_value(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def sync_submission_outcomes(supervisor: Any, events: list[Any]) -> None:
    """Forward native tool results from any platform event representation."""
    for fallback_id, event in enumerate(events):
        metadata = _event_value(event, "tool_call_metadata")
        function_name = _event_value(metadata, "function_name") if metadata else None
        if function_name != TOOL_NAME:
            continue
        content = _event_value(event, "content")
        if not isinstance(content, str) or not content.strip():
            continue
        outcome = submission_response_outcome(content)
        event_id = _event_value(event, "id")
        supervisor.observe_submission_outcome(
            event_id=event_id if event_id is not None else fallback_id,
            target_triggered=outcome is True,
            result_valid=outcome is not None,
        )


@dataclass
class TrajectorySubmissionSupervisor:
    skeleton: dict[str, Any]
    log_path: Path
    api_key: str
    inject_message: Callable[[str], None]
    judge: Callable[..., dict[str, Any]] = call_trajectory_observer
    reward_spec: dict[str, Any] | None = None
    model: str = "deepseek-chat"
    api_url: str = "https://api.deepseek.com/chat/completions"
    state: EpisodeState = EpisodeState.EXPLORING
    submission_requested: bool = False
    candidate_materialization_seen: bool = False
    awaiting_submission_outcome: bool = False
    last_target_triggered: bool | None = None
    last_submission_result_event_id: str | int | None = None
    seen_submission_result_event_ids: set[str | int] = field(default_factory=set)

    def _log(self, kind: str, **payload: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "state": self.state.value,
            "submission_requested": self.submission_requested,
            "candidate_materialization_seen": self.candidate_materialization_seen,
            "awaiting_submission_outcome": self.awaiting_submission_outcome,
            **payload,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _transition(self, state: EpisodeState, trigger: str, **payload: Any) -> None:
        previous = self.state
        self.state = state
        self.submission_requested = state == EpisodeState.SUBMISSION_REQUIRED
        self.awaiting_submission_outcome = state == EpisodeState.VERIFYING
        self._log(
            "state_transition",
            previous=previous.value,
            current=state.value,
            trigger=trigger,
            **payload,
        )

    def observe_submission_outcome(
        self,
        *,
        event_id: str | int,
        target_triggered: bool,
        result_valid: bool = True,
    ) -> None:
        """Consume one authoritative result from the native submission tool."""
        if event_id in self.seen_submission_result_event_ids:
            return
        self.seen_submission_result_event_ids.add(event_id)
        self.last_submission_result_event_id = event_id
        self.last_target_triggered = target_triggered
        self.candidate_materialization_seen = False
        self._log(
            "submission_outcome_observed",
            target_triggered=self.last_target_triggered,
            result_valid=result_valid,
            result_event_id=event_id,
        )
        self._transition(
            EpisodeState.SUCCEEDED if target_triggered else EpisodeState.REVISING,
            "authoritative_submission_outcome",
            result_valid=result_valid,
            result_event_id=event_id,
        )

    def before_action(
        self,
        proposed_event: dict[str, Any],
        raw_trajectory: str,
    ) -> bool:
        """Apply the episode protocol before allowing a proposed agent action."""
        if is_submission_action(proposed_event):
            self.last_target_triggered = None
            self._transition(EpisodeState.VERIFYING, "submission_action")
            return True
        if self.state in {EpisodeState.SUCCEEDED, EpisodeState.EXHAUSTED}:
            return True
        if self.state == EpisodeState.VERIFYING:
            self._log("submission_outcome_pending_action_allowed")
            return True
        if self.state == EpisodeState.SUBMISSION_REQUIRED:
            if is_candidate_artifact_action(proposed_event):
                if is_candidate_materialization_action(proposed_event):
                    self.candidate_materialization_seen = True
                    self._log("candidate_materialization_action_allowed")
                else:
                    self._log("candidate_artifact_action_allowed")
                return True
            if not self.candidate_materialization_seen:
                # Keep the state visible on every unrelated action, but do not
                # freeze the agent's reasoning before it has materialized an
                # experiment. Some coding models otherwise repeat the denied
                # action forever. Once a write is observed, the hard gate below
                # prevents further exploration until native submission.
                self.inject_message(SUBMIT_PENDING_MESSAGE)
                self._log("pending_action_reminded_and_allowed")
                return True
            # The binary observer has made a state decision, not a suggestion.
            # It requests submission only after the visible trajectory supports
            # a serializable runnable experiment. Keep candidate writes and the
            # native submit action agent-owned, but pause unrelated exploration
            # after the artifact has been materialized.
            self.inject_message(SUBMIT_PENDING_MESSAGE)
            self._log("non_submission_action_blocked_with_pending_request")
            return False
        if is_candidate_materialization_action(proposed_event):
            # Candidate construction is an objective readiness event.  It is
            # stronger than another semantic observer call and requires no
            # fixed action-count threshold.  Permit this action to materialize
            # the files, then gate all subsequent exploration until submission.
            self.candidate_materialization_seen = True
            self._transition(
                EpisodeState.SUBMISSION_REQUIRED,
                "candidate_materialization_intent",
            )
            self.inject_message(SUBMIT_MESSAGE)
            self._log("candidate_materialization_trigger_allowed")
            return True
        if self.state == EpisodeState.REVISING:
            # The previous candidate and its old readiness evidence remain in
            # the visible trajectory. Asking the semantic observer whether to
            # submit again before a new candidate exists lets that stale
            # evidence immediately re-trigger the gate. Keep observing every
            # action deterministically, but leave the episode in REVISING
            # until the agent actually starts materializing a new candidate.
            # That write is handled above and atomically re-arms the gate.
            self._log("revision_action_allowed_until_candidate_changes")
            return True
        if "--BEGIN AGENT OBSERVATION--" not in raw_trajectory:
            # Initial instructions describe submit.sh, but they are not
            # evidence that the target interface or representation was
            # actually inspected. This deterministic guard prevents an
            # observer from requesting submission at trajectory step zero.
            self._log("observer_deferred_no_tool_observation")
            return True

        try:
            decision = validate_binary_decision(
                self.judge(
                    self.skeleton,
                    raw_trajectory,
                    self.api_key,
                    reward_spec=self.reward_spec,
                    model=self.model,
                    api_url=self.api_url,
                )
            )
        except Exception as exc:
            self._log("observer_error_fail_open", error=f"{type(exc).__name__}: {exc}")
            return True

        self._log("observer_decision", decision=decision)
        if decision == "continue":
            return True
        self.candidate_materialization_seen = False
        self._transition(EpisodeState.SUBMISSION_REQUIRED, "reward_agent_submit")
        self.inject_message(SUBMIT_MESSAGE)
        return True

    def before_finish(self, *, fine_trace_finalization: bool) -> bool:
        if fine_trace_finalization:
            self._transition(EpisodeState.EXHAUSTED, "configured_iteration_limit")
            return True
        if self.state == EpisodeState.SUCCEEDED:
            return True
        message = SUBMIT_MESSAGE if self.submission_requested else CONTINUE_AFTER_FINISH_MESSAGE
        self.inject_message(message)
        self._log("finish_redirected")
        return False
