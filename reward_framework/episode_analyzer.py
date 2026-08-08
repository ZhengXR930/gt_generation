"""GT-blind end-of-episode metrics and evidence-bound experience analysis."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .backend import RewardAgentBackend
from .state_store import StateStore, atomic_json, utc_now


SCHEMA = Path(__file__).resolve().with_name("schemas") / "episode_analysis.json"
CATEGORIES = {
    "missing_submission", "late_submission", "duplicate_candidate_loop",
    "invalid_submission_protocol", "reward_not_followed_by_action",
    "reward_context_loss", "premature_finish", "productive_retry",
    "causal_progress", "causal_stagnation", "trigger_success",
    "instrumentation_unavailable",
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
routing, or premature termination. Use success signals as controls. Return
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
    duplicate_ratio: float
    invalid_submissions: int
    episodes_with_submission: bool
    first_submission_sequence: int | None
    first_submission_iteration: int | None
    submission_requests: int
    reward_events: int
    rewards_followed_by_subject_action: int
    distinct_retries_after_reward: int
    causal_progress_events: int
    instrumentation_unavailable_attempts: int
    early_finish_rejections: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_subject(event: dict[str, Any]) -> bool:
    source = str(event.get("source") or "").lower()
    return source in {"agent", "assistant", "coding_agent"} or (
        "agent" in source and "reward" not in source and "harness" not in source
    )


def collect_episode_metrics(store: StateStore, *, harness_version: int) -> EpisodeMetrics:
    trajectory = store.load_observation().to_dict()
    events = list(trajectory.get("events") or [])
    evidence = store.load_evidence_state().to_dict()
    attempts = list(evidence.get("attempts") or [])
    submissions = [event for event in events if event.get("kind") == "candidate_submitted"]
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
    previous = -1
    for attempt in attempts:
        length = len((attempt.get("assessment") or {}).get("longest_confirmed_prefix") or [])
        if previous >= 0 and length > previous:
            progress += 1
        previous = max(previous, length)
    distinct_after_reward = 0
    for index, attempt in enumerate(attempts[1:], start=1):
        if attempt.get("candidate_id") != attempts[index - 1].get("candidate_id"):
            distinct_after_reward += 1
    total = len(attempts)
    duplicate = sum(1 for attempt in attempts if attempt.get("duplicate_of"))
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
        terminal_reason=trajectory.get("terminal_reason"),
        trigger_success=any(bool(item.get("trigger_observed")) for item in attempts),
        trajectory_events=len(events), total_submissions=total,
        unique_candidates=total - duplicate, duplicate_submissions=duplicate,
        duplicate_ratio=(duplicate / total) if total else 0.0,
        invalid_submissions=sum(
            1 for item in events if item.get("kind") == "candidate_submission_rejected"
        ),
        episodes_with_submission=bool(submissions),
        first_submission_sequence=first_submission_sequence,
        first_submission_iteration=first_submission_iteration,
        submission_requests=sum(1 for item in events if item.get("kind") == "submission_requested"),
        reward_events=len(rewards), rewards_followed_by_subject_action=followed,
        distinct_retries_after_reward=distinct_after_reward,
        causal_progress_events=progress,
        instrumentation_unavailable_attempts=sum(
            1 for item in attempts if item.get("instrumentation_available") is False
        ),
        early_finish_rejections=sum(
            1 for item in events if item.get("kind") == "early_finish_rejected"
        ),
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
        result = {
            "created_at": utc_now(), "metrics": metrics.to_dict(),
            "assessment": raw["assessment"], "experiences": raw["experiences"],
        }
        atomic_json(store.root / "episode_experience.json", result)
        return result

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
        elif metrics.total_submissions == 0 and events:
            experiences.append({"kind": "harness_failure", "category": "missing_submission",
                                "confidence": "medium", "evidence_sequences": [events[-1].sequence]})
        elif metrics.duplicate_ratio >= 0.5:
            experiences.append({"kind": "harness_failure", "category": "duplicate_candidate_loop",
                                "confidence": "high", "evidence_sequences": evidence({"candidate_submitted"})})
        elif metrics.causal_progress_events == 0 and metrics.reward_events:
            experiences.append({"kind": "subject_failure", "category": "causal_stagnation",
                                "confidence": "medium", "evidence_sequences": evidence({"verification_completed"})})
        assessment = "successful" if metrics.trigger_success else (
            "harness_limited" if any(x["kind"] == "harness_failure" for x in experiences)
            else "subject_limited" if experiences else "unassessable"
        )
        return {"assessment": assessment, "experiences": experiences}
