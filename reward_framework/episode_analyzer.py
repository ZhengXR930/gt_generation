"""GT-blind end-of-episode metrics and evidence-bound experience analysis."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .backend import RewardAgentBackend
from .state_store import StateStore, atomic_json, utc_now


SCHEMA = Path(__file__).resolve().with_name("schemas") / "episode_analysis.json"
CATEGORIES = {
    "missing_submission", "late_submission", "duplicate_candidate_loop",
    "candidate_materialization_failure",
    "invalid_submission_protocol", "reward_not_followed_by_action",
    "reward_context_loss", "premature_finish", "productive_retry",
    "submission_context_loss",
    "causal_progress", "causal_stagnation", "trigger_success",
    "instrumentation_unavailable",
    "tool_protocol_recovery_failure",
}

TOOL_PROTOCOL_ERROR_MARKERS = (
    "missing required parameters for function",
    "is not allowed for function",
    "invalid tool call",
    "failed to parse tool",
)
UNPARSED_TOOL_MARKERS = (
    "<function=", "dsml", "<parameter=command>", "<result>", "<command>",
)
_PROTOCOL_TAG = re.compile(
    r"</?[A-Za-z0-9_:\-｜]+(?:=[A-Za-z_][A-Za-z0-9_]*)?>"
)

_CAUSAL_STAGES = ("admission", "source", "root", "propagation", "target")
_CONTROL_STAGES = (
    "activation", "availability", "consumption", "progress", "success",
)
_STAGE_PROGRESS_RANK = {
    "not_declared": -1,
    "not_reached": 0,
    "refuted": 0,
    "unresolved": 1,
    "observed_but_blocked": 2,
    "confirmed": 3,
}


def _assessment_progress_key(assessment: dict[str, Any]) -> tuple[int, ...]:
    """Order deterministic runtime assessments by causal advancement.

    Prefix length alone loses useful within-boundary movement such as Source
    changing from ``not_reached`` to ``unresolved`` after a revised candidate.
    Lexicographic stage ranks preserve the causal gate: progress at an earlier
    stage dominates observations at later stages, and a refuted predicate is
    not credited as movement toward the trigger.
    """
    if assessment.get("protocol") == "assertion-reward-v1":
        claims = [item for item in assessment.get("claims", []) if isinstance(item, dict)]
        return (
            1 if assessment.get("admission") == "confirmed" else 0,
            sum(item.get("matched_vulnerable_state") is True for item in claims),
            sum(bool(item.get("evaluated")) for item in claims),
            int(assessment.get("information_gain") or 0),
            0,
        )
    stages = assessment.get("stages") or {}
    return tuple(
        _STAGE_PROGRESS_RANK.get(str(stages.get(stage) or "not_declared"), -1)
        for stage in _CAUSAL_STAGES
    )


def _is_tool_protocol_event(event: dict[str, Any]) -> bool:
    payload = event.get("payload") or {}
    message = str(payload.get("message") or "").lower()
    if event.get("kind") == "ObservationType.ERROR":
        return any(marker in message for marker in TOOL_PROTOCOL_ERROR_MARKERS)
    return (
        event.get("kind") == "ActionType.MESSAGE"
        and _is_subject(event)
        and ("execute_bash" in message or "dsml" in message)
        and any(marker in message for marker in UNPARSED_TOOL_MARKERS)
    )


def _tool_protocol_kind(event: dict[str, Any]) -> str | None:
    if not _is_tool_protocol_event(event):
        return None
    if event.get("kind") == "ActionType.MESSAGE":
        return "unparsed_tool_intent"
    return "invalid_tool_parameters"


def collect_protocol_evidence(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return transport-only evidence with every argument value removed."""
    tag_shapes: list[list[str]] = []
    for event in events:
        if _tool_protocol_kind(event) != "unparsed_tool_intent":
            continue
        message = str((event.get("payload") or {}).get("message") or "")
        # A broad ``<...>`` extractor accidentally treats shell comparisons,
        # truncated command fragments, and paths as tag names.  Admit only the
        # identifier-only grammar used by the transport protocol.  This keeps
        # parser shape while making it impossible for a command argument or
        # sample source path to enter cross-sample memory.
        tags = _PROTOCOL_TAG.findall(message)
        # Tags preserve parser-relevant syntax but never retain command text,
        # issue prose, paths, or argument values.
        shape = [tag[:160] for tag in tags[:12]]
        if shape and shape not in tag_shapes:
            tag_shapes.append(shape)
    # Preserve a bounded variety of transport grammars. First-class submission
    # shapes are prioritized because otherwise earlier malformed shell calls
    # can fill the budget and hide the exact harness boundary that failed.
    tag_shapes.sort(
        key=lambda shape: (
            0 if any("submit_candidate" in tag for tag in shape) else 1,
            shape,
        )
    )
    tag_shapes = tag_shapes[:8]
    signatures = {
        "missing_required_parameters": 0,
        "unknown_parameter": 0,
        "invalid_tool_call": 0,
    }
    for event in events:
        if _tool_protocol_kind(event) != "invalid_tool_parameters":
            continue
        message = str((event.get("payload") or {}).get("message") or "").lower()
        if "missing required" in message:
            signatures["missing_required_parameters"] += 1
        elif "not allowed for function" in message:
            signatures["unknown_parameter"] += 1
        else:
            signatures["invalid_tool_call"] += 1
    invalid = sum(signatures.values())
    return {
        "information_boundary": "tool/parameter tag shapes only; all values removed",
        "unparsed_tool_tag_shapes": tag_shapes,
        "invalid_tool_parameter_events": invalid,
        "invalid_tool_parameter_signatures": signatures,
    }


def collect_control_plane_transactions(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project the existing trajectory into a value-free lifecycle ledger.

    This is deterministic telemetry, not another model-generated trace.  It
    deliberately omits messages, paths, arguments, source locations, candidate
    hashes, issue content, and runtime facts.  The Patcher only learns which
    control boundary produced and consumed each event.
    """
    submissions = [
        event for event in events if event.get("kind") == "candidate_submitted"
    ]
    verifications = [
        event for event in events if event.get("kind") == "verification_completed"
    ]
    requests = [
        event for event in events if event.get("kind") == "submission_requested"
    ]
    reward_messages = [
        event for event in events
        if event.get("kind") == "ActionType.MESSAGE"
        and str(event.get("source") or "").lower() == "user"
        and str((event.get("payload") or {}).get("message") or "").startswith(
            "[Runtime reward evidence]"
        )
    ]
    observer_decisions = [
        {
            "sequence": int(event["sequence"]),
            "decision": str((event.get("payload") or {}).get("decision") or ""),
            "candidate_relation_to_last_submission": str(
                (event.get("payload") or {}).get(
                    "candidate_relation_to_last_submission"
                ) or "unknown"
            ),
        }
        for event in events if event.get("kind") == "observer_decision"
    ]

    tool_actions: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") or {}
        metadata = payload.get("tool_call_metadata") or {}
        if metadata.get("function_name") != "submit_candidate":
            continue
        if event.get("kind") == "ActionType.RUN":
            tool_actions.append({
                "sequence": int(event["sequence"]),
                "response_form": (
                    "native_or_normalized_tool_call"
                    if metadata.get("tool_call_id") else "unknown"
                ),
                "action_created": True,
                "blocking_requested": bool(
                    (payload.get("args") or {}).get("blocking")
                ),
            })
        elif event.get("kind") == "ObservationType.RUN":
            extras = payload.get("extras") or {}
            runtime_metadata = extras.get("metadata") or {}
            exit_code = runtime_metadata.get("exit_code")
            error_match = re.search(
                r'\b([A-Za-z][A-Za-z0-9_]*(?:Error|Exception))\b',
                str(payload.get("content") or ""),
            )
            tool_results.append({
                "sequence": int(event["sequence"]),
                "action_sequence": int(payload.get("cause") or 0) or None,
                "completed": True,
                "error_type": error_match.group(1) if error_match else None,
                "exit_class": (
                    "success" if exit_code == 0
                    else "transport_or_framework_error" if exit_code == 3
                    else "subject_input_error" if exit_code == 2
                    else "other_failure"
                ),
            })

    transactions: list[dict[str, Any]] = []
    for index, submission in enumerate(submissions):
        sequence = int(submission["sequence"])
        payload = submission.get("payload") or {}
        next_submission_sequence = (
            int(submissions[index + 1]["sequence"])
            if index + 1 < len(submissions) else 10**18
        )
        attempt_number = int(payload.get("attempt_number") or index + 1)
        verification = next((
            event for event in verifications
            if int((event.get("payload") or {}).get("attempt_number") or -1)
            == attempt_number
        ), None)
        verification_sequence = (
            int(verification["sequence"]) if verification is not None else None
        )
        reward_delivery = next((
            event for event in reward_messages
            if verification_sequence is not None
            and verification_sequence < int(event["sequence"])
            < next_submission_sequence
        ), None)
        reward_sequence = (
            int(reward_delivery["sequence"])
            if reward_delivery is not None else None
        )
        subject_acted_after_reward = bool(
            reward_sequence is not None and any(
                reward_sequence < int(event["sequence"]) < next_submission_sequence
                and _is_subject(event)
                for event in events
            )
        )
        transactions.append({
            "attempt_number": attempt_number,
            "submission_sequence": sequence,
            "reminder_preceded_submission": any(
                int(request["sequence"]) < sequence
                and (
                    index == 0
                    or int(request["sequence"]) > int(submissions[index - 1]["sequence"])
                )
                for request in requests
            ),
            "candidate_registered": True,
            "duplicate_candidate": bool(payload.get("duplicate_of")),
            "verification_completed": verification is not None,
            "reward_delivered": reward_delivery is not None,
            "subject_acted_after_reward_before_next_submission": (
                subject_acted_after_reward
            ),
            "next_submission_is_distinct": (
                None if index + 1 >= len(submissions)
                else not bool(
                    (submissions[index + 1].get("payload") or {}).get("duplicate_of")
                )
            ),
        })

    stale_requests = 0
    for request in requests:
        request_sequence = int(request["sequence"])
        prior_rewards = [
            int(event["sequence"]) for event in reward_messages
            if int(event["sequence"]) < request_sequence
        ]
        if not prior_rewards:
            continue
        last_reward = max(prior_rewards)
        if not any(
            last_reward < int(event["sequence"]) < request_sequence
            and _is_subject(event)
            for event in events
        ):
            stale_requests += 1

    return {
        "information_boundary": (
            "control states and value-free tool lifecycle only; semantic text, "
            "arguments, paths, hashes, source, issue, PoC, and runtime facts removed"
        ),
        "submission_tool_actions": tool_actions,
        "submission_tool_results": tool_results,
        "observer_decisions": observer_decisions,
        "submission_transactions": transactions,
        "submission_requests_after_reward_without_subject_action": stale_requests,
    }

ANALYZER_PROMPT = """You are the end-of-episode Experience Analyzer. Read
episode_bundle.json. This is a completed vulnerability-reproduction episode.
The public issue is authoritative, Subject reasoning is untrusted, and runtime
facts are authoritative when instrumentation_available is true.

Classify evidence, not vulnerability content. Distinguish Subject limitations
from harness limitations. A candidate that fails to establish Root is not by
itself a harness failure. A harness failure requires trajectory evidence such
as missing/late submission despite a runnable candidate, repeated identical
candidates after feedback, loss of trusted Reward facts, invalid submission
routing, or premature termination. Repeated semantic supervisor reminders with
no later candidate submission are specifically a candidate-materialization
failure; do not infer this failure when no reminder was issued. Use success
signals as controls. Return
only enumerated categories and exact trajectory sequence numbers. Do not emit
source locations, variable names, issue text, PoC details, advice, or prose.
Ground truth, known crashes, and held-out evaluation are unavailable.
"""


@dataclass(frozen=True)
class EpisodeMetrics:
    harness_version: int
    terminal_reason: str | None
    trigger_success: bool
    trajectory_events: int
    total_submissions: int
    unique_candidates: int
    duplicate_submissions: int
    duplicate_submissions_without_feedback_action: int
    duplicate_ratio: float
    invalid_submissions: int
    episodes_with_submission: bool
    first_submission_sequence: int | None
    first_submission_iteration: int | None
    submission_requests: int
    semantic_submission_requests: int
    suppressed_duplicate_reminders: int
    supervisor_reminders: int
    materialization_reminders: int
    ready_submission_reminders: int
    reminder_triggered_submissions: int
    post_reminder_no_submission: bool
    condensation_events: int
    pending_submission_condensations: int
    post_condensation_no_submission: bool
    reward_events: int
    rewards_followed_by_subject_action: int
    distinct_retries_after_reward: int
    causal_progress_events: int
    instrumentation_unavailable_attempts: int
    early_finish_rejections: int
    tool_protocol_errors: int
    invalid_tool_parameter_errors: int
    unparsed_tool_intents: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_stage_control(
    store: StateStore, metrics: EpisodeMetrics,
) -> dict[str, Any]:
    """Deterministically locate the first broken Reward control boundary.

    This is the single bridge between task-local causal evidence and
    cross-sample harness optimization.  It does not interpret issue prose or
    Subject reasoning: each transition is justified only by persisted
    controller/runtime events.  Downstream stages are ``not_reached`` once an
    upstream boundary is blocked, so the Patcher cannot optimize a consequence
    while ignoring its cause.
    """
    trajectory = store.load_observation().to_dict()
    events = list(trajectory.get("events") or [])
    evidence = store.load_evidence_state().to_dict()
    attempts = list(evidence.get("attempts") or [])

    def sequences(*kinds: str) -> list[int]:
        wanted = set(kinds)
        return [
            int(event["sequence"]) for event in events
            if event.get("kind") in wanted
        ][-12:]

    stages: dict[str, dict[str, Any]] = {}

    if metrics.total_submissions:
        stages["activation"] = {
            "status": "complete",
            "reason": "a candidate crossed the first-class submission boundary",
            "evidence_sequences": sequences("candidate_submitted")[:1],
        }
    else:
        activation_evidence = sequences(
            "observer_submission_deferred", "submission_requested",
            "observer_materialization_still_pending",
            "candidate_submission_rejected", "controller_iteration",
        )
        stages["activation"] = {
            "status": "blocked",
            "reason": "the completed episode produced no registered candidate",
            "evidence_sequences": activation_evidence,
        }

    usable_attempts = [
        attempt for attempt in attempts
        if bool(attempt.get("instrumentation_available"))
        or bool(attempt.get("trigger_observed"))
    ]
    if stages["activation"]["status"] != "complete":
        stages["availability"] = {
            "status": "not_reached",
            "reason": "no submitted candidate was available to verify",
            "evidence_sequences": [],
        }
    elif usable_attempts:
        stages["availability"] = {
            "status": "complete",
            "reason": "candidate verification produced usable runtime evidence",
            "evidence_sequences": sequences("verification_completed"),
        }
    else:
        stages["availability"] = {
            "status": "blocked",
            "reason": "submissions produced no usable instrumented runtime evidence",
            "evidence_sequences": sequences(
                "candidate_submitted", "verification_completed"
            ),
        }

    if metrics.trigger_success and not metrics.distinct_retries_after_reward:
        # A first candidate may succeed before feedback/retry is needed. These
        # transitions are bypassed, not failures. If success follows a distinct
        # retry, however, Consumption and Progress are real completed stages
        # and must remain visible as evidence for the Reward loop.
        for stage in ("consumption", "progress"):
            stages[stage] = {
                "status": "not_required",
                "reason": "the trigger succeeded before another retry was required",
                "evidence_sequences": [],
            }
    elif stages["availability"]["status"] != "complete":
        stages["consumption"] = {
            "status": "not_reached",
            "reason": "usable Reward evidence was not available for consumption",
            "evidence_sequences": [],
        }
        stages["progress"] = {
            "status": "not_reached",
            "reason": "no evidence-consuming retry was available to assess",
            "evidence_sequences": [],
        }
    elif metrics.distinct_retries_after_reward:
        stages["consumption"] = {
            "status": "complete",
            "reason": "runtime Reward was followed by a distinct candidate retry",
            "evidence_sequences": sequences(
                "verification_completed", "candidate_submitted"
            ),
        }
        if metrics.causal_progress_events or metrics.trigger_success:
            stages["progress"] = {
                "status": "complete",
                "reason": (
                    "a distinct retry produced the successful trigger"
                    if metrics.trigger_success
                    else "a distinct retry advanced the causal assessment frontier"
                ),
                "evidence_sequences": sequences("verification_completed"),
            }
        else:
            stages["progress"] = {
                "status": "blocked",
                "reason": "distinct retries did not advance the causal frontier",
                "evidence_sequences": sequences("verification_completed"),
            }
    else:
        stages["consumption"] = {
            "status": "blocked",
            "reason": "runtime Reward was not converted into a distinct retry",
            "evidence_sequences": sequences(
                "verification_completed", "candidate_submitted"
            ),
        }
        stages["progress"] = {
            "status": "not_reached",
            "reason": "no distinct evidence-consuming retry was available",
            "evidence_sequences": [],
        }

    if metrics.trigger_success:
        stages["success"] = {
            "status": "complete",
            "reason": "the independent trigger oracle succeeded",
            "evidence_sequences": sequences("verification_completed"),
        }
    elif stages["progress"]["status"] == "complete":
        stages["success"] = {
            "status": "blocked",
            "reason": "causal progress did not yet produce a successful trigger",
            "evidence_sequences": sequences("verification_completed"),
        }
    else:
        stages["success"] = {
            "status": "not_reached",
            "reason": "an earlier control boundary prevented a successful trigger",
            "evidence_sequences": [],
        }

    first_blocked = next(
        (stage for stage in _CONTROL_STAGES
         if stages[stage]["status"] == "blocked"),
        None,
    )
    completed_prefix: list[str] = []
    for stage in _CONTROL_STAGES:
        if stages[stage]["status"] in {"complete", "not_required"}:
            completed_prefix.append(stage)
            continue
        break
    return {
        "schema_version": 1,
        "stage_order": list(_CONTROL_STAGES),
        "stages": stages,
        "completed_prefix": completed_prefix,
        "first_blocked_stage": first_blocked,
    }


def _is_subject(event: dict[str, Any]) -> bool:
    source = str(event.get("source") or "").lower()
    return source in {"agent", "assistant", "coding_agent"} or (
        "agent" in source and "reward" not in source and "harness" not in source
    )


def _condensation_preserves_submission_obligation(event: dict[str, Any]) -> bool:
    """Recognize the task-neutral memory contract added by the harness.

    Merely seeing no submission after condensation is not evidence of context
    loss. The summary itself must have dropped the pending first-class action.
    Candidate bytes, issue prose, and source details are deliberately ignored.
    """
    payload = event.get("payload") or {}
    summary = str(payload.get("message") or payload.get("summary") or "").lower()
    return "submission_state" in summary and "submit_candidate" in summary


def _observed_terminal_reason(
    trajectory: dict[str, Any], events: list[dict[str, Any]]
) -> str | None:
    reason = trajectory.get("terminal_reason")
    if reason:
        return str(reason)
    # A tool-free fine-trace finalization can make the final controller state
    # look FINISHED even when the Subject episode was terminated by OpenHands.
    # Recover the controller-owned reason recorded in that finalization event.
    for event in reversed(events):
        if event.get("kind") != "ActionType.MESSAGE":
            continue
        message = str((event.get("payload") or {}).get("message") or "")
        lowered = message.lower()
        if "[fine trace finalization]" not in lowered:
            continue
        if "agentstuckinlooperror" in lowered:
            return "agent_stuck_loop"
        if "agent_error:" in lowered:
            return "agent_error"
        if "iteration" in lowered and "limit" in lowered:
            return "iteration_limit"
    return None


def collect_episode_metrics(store: StateStore, *, harness_version: int) -> EpisodeMetrics:
    trajectory = store.load_observation().to_dict()
    events = list(trajectory.get("events") or [])
    evidence = store.load_evidence_state().to_dict()
    attempts = list(evidence.get("attempts") or [])
    tool_protocol_errors = [event for event in events if _is_tool_protocol_event(event)]
    protocol_kinds = [_tool_protocol_kind(event) for event in tool_protocol_errors]
    submissions = [event for event in events if event.get("kind") == "candidate_submitted"]
    materialization_reminders = [
        event for event in events
        if event.get("kind") == "observer_submission_deferred"
    ]
    ready_submission_reminders = [
        event for event in events if event.get("kind") == "submission_requested"
    ]
    reminders = sorted(
        materialization_reminders + ready_submission_reminders,
        key=lambda event: int(event["sequence"]),
    )
    semantic_submission_requests = [
        event for event in events
        if event.get("kind") == "observer_decision"
        and (event.get("payload") or {}).get("decision") == "request_submission"
    ]
    suppressed_duplicate_reminders = [
        event for event in events
        if event.get("kind") == "observer_materialization_still_pending"
    ]
    condensations = [
        event for event in events
        if event.get("kind") == "ActionType.CONDENSATION"
    ]
    pending_condensations = []
    for condensation in condensations:
        sequence = int(condensation["sequence"])
        earlier_reminders = [
            int(item["sequence"]) for item in reminders
            if int(item["sequence"]) < sequence
        ]
        if not earlier_reminders:
            continue
        latest_reminder = max(earlier_reminders)
        if not any(
            latest_reminder < int(item["sequence"]) < sequence
            for item in submissions
        ):
            pending_condensations.append(condensation)
    lost_pending_condensations = [
        condensation for condensation in pending_condensations
        if not _condensation_preserves_submission_obligation(condensation)
    ]
    reminder_triggered_submissions = 0
    previous_submission = -1
    for submission in submissions:
        sequence = int(submission["sequence"])
        if any(
            previous_submission < int(reminder["sequence"]) < sequence
            for reminder in reminders
        ):
            reminder_triggered_submissions += 1
        previous_submission = sequence
    rewards = [event for event in events if event.get("kind") == "verification_completed"]
    followed = 0
    for reward in rewards:
        sequence = int(reward["sequence"])
        next_reward = min(
            (int(item["sequence"]) for item in rewards if int(item["sequence"]) > sequence),
            default=10**18,
        )
        if any(
            sequence < int(item["sequence"]) < next_reward and _is_subject(item)
            for item in events
        ):
            followed += 1
    progress = 0
    previous: tuple[int, ...] | None = None
    for attempt in attempts:
        current = _assessment_progress_key(attempt.get("assessment") or {})
        if previous is not None and current > previous:
            progress += 1
        if previous is None or current > previous:
            previous = current
    distinct_after_reward = 0
    for index, attempt in enumerate(attempts[1:], start=1):
        if attempt.get("candidate_id") != attempts[index - 1].get("candidate_id"):
            distinct_after_reward += 1
    total = len(attempts)
    duplicate = sum(1 for attempt in attempts if attempt.get("duplicate_of"))
    duplicate_without_feedback_action = 0
    for submission in submissions:
        payload = submission.get("payload") or {}
        if not payload.get("duplicate_of"):
            continue
        sequence = int(submission["sequence"])
        previous_verifications = [
            int(event["sequence"]) for event in rewards
            if int(event["sequence"]) < sequence
        ]
        if not previous_verifications:
            duplicate_without_feedback_action += 1
            continue
        boundary = max(previous_verifications)
        if not any(
            boundary < int(event["sequence"]) < sequence
            and _is_subject(event)
            for event in events
        ):
            # The next identical submission crossed the boundary before the
            # Subject had any action in which it could consume runtime Reward.
            # This is a submission synchronization/harness defect, not weak
            # candidate reasoning.
            duplicate_without_feedback_action += 1
    first_submission_sequence = int(submissions[0]["sequence"]) if submissions else None
    first_submission_iteration = None
    if first_submission_sequence is not None:
        ticks = [
            item for item in events
            if item.get("kind") == "controller_iteration"
            and int(item["sequence"]) <= first_submission_sequence
        ]
        if ticks:
            first_submission_iteration = int(ticks[-1]["payload"]["iteration"])
    return EpisodeMetrics(
        harness_version=harness_version,
        terminal_reason=_observed_terminal_reason(trajectory, events),
        trigger_success=any(bool(item.get("trigger_observed")) for item in attempts),
        trajectory_events=len(events), total_submissions=total,
        unique_candidates=total - duplicate, duplicate_submissions=duplicate,
        duplicate_submissions_without_feedback_action=(
            duplicate_without_feedback_action
        ),
        duplicate_ratio=(duplicate / total) if total else 0.0,
        invalid_submissions=sum(
            1 for item in events if item.get("kind") == "candidate_submission_rejected"
        ),
        episodes_with_submission=bool(submissions),
        first_submission_sequence=first_submission_sequence,
        first_submission_iteration=first_submission_iteration,
        submission_requests=sum(1 for item in events if item.get("kind") == "submission_requested"),
        semantic_submission_requests=len(semantic_submission_requests),
        suppressed_duplicate_reminders=len(suppressed_duplicate_reminders),
        supervisor_reminders=len(reminders),
        materialization_reminders=len(materialization_reminders),
        ready_submission_reminders=len(ready_submission_reminders),
        reminder_triggered_submissions=reminder_triggered_submissions,
        post_reminder_no_submission=bool(reminders and not submissions),
        condensation_events=len(condensations),
        pending_submission_condensations=len(pending_condensations),
        post_condensation_no_submission=bool(
            lost_pending_condensations
            and not any(
                int(item["sequence"]) > int(lost_pending_condensations[-1]["sequence"])
                for item in submissions
            )
        ),
        reward_events=len(rewards), rewards_followed_by_subject_action=followed,
        distinct_retries_after_reward=distinct_after_reward,
        causal_progress_events=progress,
        instrumentation_unavailable_attempts=sum(
            1 for item in attempts if item.get("instrumentation_available") is False
        ),
        early_finish_rejections=sum(
            1 for item in events if item.get("kind") == "early_finish_rejected"
        ),
        tool_protocol_errors=len(tool_protocol_errors),
        invalid_tool_parameter_errors=protocol_kinds.count("invalid_tool_parameters"),
        unparsed_tool_intents=protocol_kinds.count("unparsed_tool_intent"),
    )


class EpisodeAnalyzer:
    def __init__(self, backend: RewardAgentBackend):
        self.backend = backend

    def analyze(self, *, store: StateStore, harness_version: int) -> dict[str, Any]:
        metrics = collect_episode_metrics(store, harness_version=harness_version)
        bundle = store.global_state()
        bundle["episode_metrics"] = metrics.to_dict()
        bundle["information_boundary"] = "public issue + source + trajectory + runtime only; no GT"
        path = store.root / "agent_view" / "episode_bundle.json"
        atomic_json(path, bundle)
        try:
            raw = self.backend.run_json(
                role="analyze_episode", prompt=ANALYZER_PROMPT,
                schema=SCHEMA, cwd=store.root / "agent_view",
            )
            self._validate(raw, store)
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError):
            raw = self._fallback(metrics, store)
        raw = self._enforce_deterministic_categories(raw, metrics, store)
        result = {
            "created_at": utc_now(), "metrics": metrics.to_dict(),
            "assessment": raw["assessment"], "experiences": raw["experiences"],
            "harness_evidence": collect_protocol_evidence(
                store.load_observation().to_dict().get("events") or []
            ),
            "control_plane": collect_control_plane_transactions(
                store.load_observation().to_dict().get("events") or []
            ),
            "stage_control": build_stage_control(store, metrics),
        }
        atomic_json(store.root / "episode_experience.json", result)
        return result

    @staticmethod
    def _enforce_deterministic_categories(
        raw: dict[str, Any], metrics: EpisodeMetrics, store: StateStore
    ) -> dict[str, Any]:
        """Keep mechanically provable harness failures out of LLM discretion."""
        experiences = list(raw.get("experiences") or [])
        if metrics.trigger_success:
            # A confirmed independent trigger is mechanically incompatible with
            # the episode-level diagnosis that causal work stagnated.  The
            # analyzer may still retain concrete harness failures (for example,
            # invalid or duplicate submissions before eventual success), but it
            # must not turn a successful episode into a mixed result merely
            # because passive stage attribution was incomplete.
            experiences = [
                item for item in experiences
                if item.get("category") != "causal_stagnation"
            ]
        else:
            experiences = [
                item for item in experiences
                if item.get("category") != "trigger_success"
            ]
        if metrics.causal_progress_events == 0:
            experiences = [
                item for item in experiences
                if item.get("category") != "causal_progress"
            ]
        if metrics.distinct_retries_after_reward == 0:
            experiences = [
                item for item in experiences
                if item.get("category") != "productive_retry"
            ]
        if metrics.total_submissions < 2:
            experiences = [
                item for item in experiences
                if item.get("category") != "duplicate_candidate_loop"
            ]
        if metrics.post_reminder_no_submission:
            # This category is defined entirely by controller-owned events.  An
            # Analyzer may add useful Subject diagnoses, but cannot relabel the
            # failed reminder-to-candidate transition as a Subject-only error.
            experiences = [
                item for item in experiences
                if item.get("category") not in {
                    "candidate_materialization_failure", "missing_submission",
                }
            ]
            sequences = [
                event.sequence for event in store.load_observation().events
                if event.kind in {
                    "observer_submission_deferred", "submission_requested",
                }
            ][-12:]
            experiences.insert(0, {
                "kind": "harness_failure",
                "category": "candidate_materialization_failure",
                # The semantic readiness judgment can be uncertain, but the
                # failed controller transition is not: a completed episode
                # received the task-neutral materialization request and never
                # crossed the first-class submission boundary.  Keeping this
                # at medium confidence prevents the Patcher from ever fixing
                # the mechanism that must activate Reward in the first place.
                "confidence": "high",
                "evidence_sequences": sequences,
            })
        if metrics.tool_protocol_errors >= 2:
            experiences = [
                item for item in experiences
                if item.get("category") != "tool_protocol_recovery_failure"
            ]
            sequences = [
                event.sequence for event in store.load_observation().events
                if _is_tool_protocol_event({
                    "source": event.source,
                    "kind": event.kind,
                    "payload": event.payload,
                })
            ][-12:]
            # An unambiguous text/DSML action that OpenHands failed to recover
            # belongs to the harness.  Missing required arguments are not
            # recoverable without inventing Subject intent, so retain them as
            # a Subject protocol failure rather than asking the Patcher to
            # guess a command or candidate.
            experiences.insert(0, {
                "kind": (
                    "harness_failure"
                    if metrics.unparsed_tool_intents >= 2
                    else "subject_failure"
                ),
                "category": "tool_protocol_recovery_failure",
                "confidence": "high",
                "evidence_sequences": sequences,
            })
        if metrics.terminal_reason == "agent_stuck_loop":
            experiences = [
                item for item in experiences
                if item.get("category") != "premature_finish"
            ]
            sequences = [
                event.sequence for event in store.load_observation().events
                if event.kind in {"controller_iteration", "ActionType.MESSAGE"}
                and (
                    event.kind == "controller_iteration"
                    or "agentstuckinlooperror" in str(
                        event.payload.get("message") or ""
                    ).lower()
                )
            ][-2:]
            experiences.insert(0, {
                "kind": "harness_failure",
                "category": "premature_finish",
                "confidence": "high",
                "evidence_sequences": sequences,
            })
        if metrics.post_condensation_no_submission:
            experiences = [
                item for item in experiences
                if item.get("category") != "submission_context_loss"
            ]
            sequences = [
                event.sequence for event in store.load_observation().events
                if event.kind in {
                    "observer_submission_deferred", "submission_requested",
                    "ActionType.CONDENSATION",
                }
            ][-12:]
            experiences.insert(0, {
                "kind": "harness_failure",
                "category": "submission_context_loss",
                "confidence": "high",
                "evidence_sequences": sequences,
            })
        else:
            # Context loss is a controller-observable memory-boundary failure,
            # not a semantic judgment. If the condensed summary preserved the
            # first-class obligation, later inaction belongs to candidate
            # materialization rather than memory loss.
            experiences = [
                item for item in experiences
                if item.get("category") != "submission_context_loss"
            ]
        if metrics.instrumentation_unavailable_attempts == 0:
            # Instrumentation cannot be unavailable when it was never invoked.
            # Remove this common Analyzer hallucination before it reaches the
            # cross-sample optimizer.
            experiences = [
                item for item in experiences
                if item.get("category") != "instrumentation_unavailable"
            ]
        if metrics.duplicate_ratio >= 0.5 and metrics.total_submissions >= 2:
            experiences = [
                item for item in experiences
                if item.get("category") != "duplicate_candidate_loop"
            ]
            sequences = [
                event.sequence for event in store.load_observation().events
                if event.kind == "candidate_submitted"
            ][-12:]
            harness_owned = (
                metrics.duplicate_submissions_without_feedback_action > 0
            )
            experiences.append({
                "kind": "harness_failure" if harness_owned else "subject_failure",
                "category": "duplicate_candidate_loop",
                "confidence": "high",
                "evidence_sequences": sequences,
            })
        if (
            metrics.terminal_reason == "iteration_limit"
            and metrics.total_submissions == 0
        ):
            experiences = [
                item for item in experiences
                if item.get("category") != "missing_submission"
            ]
            events = store.load_observation().events
            sequences = [
                event.sequence for event in events
                if event.kind in {"controller_iteration", "iteration_limit_reached"}
            ][-2:]
            if not sequences and events:
                sequences = [events[-1].sequence]
            experiences.append({
                "kind": "harness_failure",
                "category": "missing_submission",
                "confidence": "high",
                "evidence_sequences": sequences,
            })
        has_harness = any(
            item.get("kind") == "harness_failure" for item in experiences
        )
        has_subject = any(
            item.get("kind") == "subject_failure" for item in experiences
        )
        assessment = raw.get("assessment", "unassessable")
        if has_harness:
            assessment = "mixed" if has_subject else "harness_limited"
        elif metrics.trigger_success:
            assessment = "successful"
        return {"assessment": assessment, "experiences": experiences}

    @staticmethod
    def _validate(raw: dict[str, Any], store: StateStore) -> None:
        if set(raw) != {"assessment", "experiences"}:
            raise ValueError("episode analysis returned unexpected fields")
        valid_sequences = {
            event.sequence for event in store.load_observation().events
        }
        for item in raw.get("experiences") or []:
            if set(item) != {"kind", "category", "confidence", "evidence_sequences"}:
                raise ValueError("invalid experience fields")
            if item["category"] not in CATEGORIES:
                raise ValueError("invalid experience category")
            sequences = item["evidence_sequences"]
            if not sequences or any(int(value) not in valid_sequences for value in sequences):
                raise ValueError("experience cites a nonexistent trajectory event")

    @staticmethod
    def _fallback(metrics: EpisodeMetrics, store: StateStore) -> dict[str, Any]:
        events = store.load_observation().events
        evidence = lambda kinds: [
            event.sequence for event in events if event.kind in kinds
        ][-6:]
        experiences: list[dict[str, Any]] = []
        if metrics.trigger_success:
            experiences.append({"kind": "success_signal", "category": "trigger_success",
                                "confidence": "high", "evidence_sequences": evidence({"verification_completed"})})
        elif metrics.invalid_submissions:
            experiences.append({
                "kind": "harness_failure", "category": "invalid_submission_protocol",
                "confidence": "high",
                "evidence_sequences": evidence({"candidate_submission_rejected"}),
            })
        elif metrics.post_reminder_no_submission:
            experiences.append({
                "kind": "harness_failure",
                "category": "candidate_materialization_failure",
                "confidence": "high",
                "evidence_sequences": evidence({
                    "observer_submission_deferred", "submission_requested",
                }),
            })
        elif (
            metrics.total_submissions == 0
            and metrics.terminal_reason is not None
            and events
        ):
            experiences.append({"kind": "harness_failure", "category": "missing_submission",
                                "confidence": "medium", "evidence_sequences": [events[-1].sequence]})
        if metrics.tool_protocol_errors >= 2:
            sequences = [
                event.sequence for event in events
                if _is_tool_protocol_event({
                    "source": event.source,
                    "kind": event.kind,
                    "payload": event.payload,
                })
            ][-12:]
            experiences.insert(0, {
                "kind": (
                    "harness_failure"
                    if metrics.unparsed_tool_intents >= 2
                    else "subject_failure"
                ),
                "category": "tool_protocol_recovery_failure",
                "confidence": "high",
                "evidence_sequences": sequences,
            })
        if metrics.post_condensation_no_submission:
            experiences.insert(0, {
                "kind": "harness_failure",
                "category": "submission_context_loss",
                "confidence": "high",
                "evidence_sequences": evidence({
                    "observer_submission_deferred", "submission_requested",
                    "ActionType.CONDENSATION",
                }),
            })
        if metrics.instrumentation_unavailable_attempts == 0:
            experiences = [
                item for item in experiences
                if item.get("category") != "instrumentation_unavailable"
            ]
        if metrics.duplicate_ratio >= 0.5 and metrics.total_submissions >= 2:
            experiences.append({
                "kind": (
                    "harness_failure"
                    if metrics.duplicate_submissions_without_feedback_action > 0
                    else "subject_failure"
                ),
                "category": "duplicate_candidate_loop",
                "confidence": "high",
                "evidence_sequences": evidence({"candidate_submitted"}),
            })
        elif metrics.causal_progress_events == 0 and metrics.reward_events:
            experiences.append({"kind": "subject_failure", "category": "causal_stagnation",
                                "confidence": "medium", "evidence_sequences": evidence({"verification_completed"})})
        assessment = "successful" if metrics.trigger_success else (
            "harness_limited" if any(x["kind"] == "harness_failure" for x in experiences)
            else "subject_limited" if experiences else "unassessable"
        )
        return {"assessment": assessment, "experiences": experiences}
