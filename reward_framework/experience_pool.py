"""Append-only, GT-free cross-episode experience memory."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .state_store import atomic_json


METRIC_KEYS = (
    "trigger_success", "total_submissions", "unique_candidates",
    "duplicate_submissions", "duplicate_submissions_without_feedback_action",
    "duplicate_ratio", "invalid_submissions",
    "episodes_with_submission", "first_submission_iteration", "submission_requests",
    "semantic_submission_requests", "suppressed_duplicate_reminders",
    "supervisor_reminders", "materialization_reminders",
    "ready_submission_reminders", "reminder_triggered_submissions",
    "post_reminder_no_submission",
    "reward_events", "rewards_followed_by_subject_action",
    "distinct_retries_after_reward", "causal_progress_events",
    "instrumentation_unavailable_attempts", "early_finish_rejections",
    "tool_protocol_errors",
    "invalid_tool_parameter_errors", "unparsed_tool_intents",
)


class ExperiencePool:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.episodes = self.root / "episodes"
        self.trajectories = self.root / "trajectories"
        self.lock_path = self.root / ".lock"
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return {"schema_version": 1, "episode_count": 0, "episodes": [],
                    "by_harness_version": {}, "category_counts": {}}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def append(
        self, experience: dict[str, Any], *, trajectory: dict[str, Any] | None = None
    ) -> str:
        encoded = json.dumps(
            {"experience": experience, "trajectory": trajectory},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        episode_id = "episode_" + hashlib.sha256(encoded).hexdigest()[:20]
        with self.locked():
            index = self._index()
            if episode_id in index["episodes"]:
                return episode_id
            self.episodes.mkdir(parents=True, exist_ok=True)
            atomic_json(self.episodes / f"{episode_id}.json", experience)
            if trajectory is not None:
                self.trajectories.mkdir(parents=True, exist_ok=True)
                atomic_json(
                    self.trajectories / f"{episode_id}.json", trajectory
                )
            index["episodes"].append(episode_id)
            index["episode_count"] = len(index["episodes"])
            metrics = experience["metrics"]
            version = str(metrics["harness_version"])
            aggregate = index["by_harness_version"].setdefault(
                version, {"episodes": 0, **{key: 0 for key in METRIC_KEYS}}
            )
            aggregate["episodes"] += 1
            for key in METRIC_KEYS:
                # Existing training pools predate reminder-conversion metrics.
                aggregate.setdefault(key, 0)
                aggregate[key] += float(metrics.get(key) or 0)
            for item in experience.get("experiences") or []:
                category = item["category"]
                index["category_counts"][category] = (
                    index["category_counts"].get(category, 0) + 1
                )
            atomic_json(self.index_path, index)
        return episode_id

    def load_trajectory(self, episode_id: str) -> dict[str, Any] | None:
        """Load the frozen canonical trajectory snapshot for one pool episode."""
        if not re.fullmatch(r"episode_[0-9a-f]{20}", episode_id):
            raise ValueError("invalid experience-pool episode id")
        path = self.trajectories / f"{episode_id}.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("events"), list):
            raise ValueError("invalid canonical trajectory snapshot")
        return value

    def optimizer_view(self, *, max_episode_cards: int = 64) -> dict[str, Any]:
        """Generalized experiences only: no task id, issue, source, trace, or GT."""
        with self.locked():
            index = self._index()
            # Aggregates cover the full history. Cards are a bounded working set:
            # recent behavior plus at least one representative for every category.
            recent = list(index["episodes"][-max_episode_cards:])
            representatives: dict[str, str] = {}
            cached: dict[str, dict[str, Any]] = {}
            for episode_id in reversed(index["episodes"]):
                value = json.loads(
                    (self.episodes / f"{episode_id}.json").read_text(encoding="utf-8")
                )
                cached[episode_id] = value
                for item in value.get("experiences") or []:
                    representatives.setdefault(item["category"], episode_id)
            representative_ids = list(dict.fromkeys(representatives.values()))
            remaining = max(0, max_episode_cards - len(representative_ids))
            recent_non_representatives = [
                episode_id for episode_id in recent
                if episode_id not in representative_ids
            ]
            selected = representative_ids + (
                recent_non_representatives[-remaining:] if remaining else []
            )
            episodes = []
            for episode_id in selected:
                value = cached.get(episode_id) or json.loads(
                    (self.episodes / f"{episode_id}.json").read_text(encoding="utf-8")
                )
                episodes.append({
                    "episode_id": episode_id,
                    "metrics": value["metrics"],
                    "assessment": value["assessment"],
                    "experiences": value["experiences"],
                    "harness_evidence": value.get("harness_evidence", {}),
                    "control_plane": value.get("control_plane", {}),
                    "stage_control": value.get("stage_control", {}),
                    "trajectory_file": (
                        f"trajectories/{episode_id}.json"
                        if (self.trajectories / f"{episode_id}.json").is_file()
                        else None
                    ),
                })
            objectives = {}
            for version, totals in index["by_harness_version"].items():
                episodes_count = max(1.0, float(totals["episodes"]))
                submissions = float(totals["total_submissions"])
                rewards = float(totals["reward_events"])
                reminders = float(totals.get("supervisor_reminders") or 0)
                semantic_requests = float(
                    totals.get("semantic_submission_requests") or 0
                )
                episodes_with_submission = float(totals["episodes_with_submission"])
                objectives[version] = {
                    "episodes": int(totals["episodes"]),
                    "trigger_rate": float(totals["trigger_success"]) / episodes_count,
                    "no_submission_rate": 1.0 - episodes_with_submission / episodes_count,
                    "supervisor_reminders_per_episode": reminders / episodes_count,
                    "semantic_submission_requests_per_episode": (
                        semantic_requests / episodes_count
                    ),
                    "suppressed_duplicate_reminders_per_episode": (
                        float(totals.get("suppressed_duplicate_reminders") or 0)
                        / episodes_count
                    ),
                    "reminder_submission_conversion_rate": (
                        float(totals.get("reminder_triggered_submissions") or 0)
                        / reminders if reminders else None
                    ),
                    "post_reminder_no_submission_rate": (
                        float(totals.get("post_reminder_no_submission") or 0)
                        / episodes_count
                    ),
                    "mean_first_submission_iteration": (
                        float(totals["first_submission_iteration"])
                        / episodes_with_submission
                        if episodes_with_submission else None
                    ),
                    "duplicate_submission_rate": (
                        float(totals["duplicate_submissions"]) / submissions
                        if submissions else 0.0
                    ),
                    "feedback_bypass_duplicate_rate": (
                        float(totals.get(
                            "duplicate_submissions_without_feedback_action"
                        ) or 0) / submissions
                        if submissions else 0.0
                    ),
                    "invalid_submission_rate": (
                        float(totals["invalid_submissions"])
                        / (submissions + float(totals["invalid_submissions"]))
                        if submissions + float(totals["invalid_submissions"]) else 0.0
                    ),
                    "reward_followup_rate": (
                        float(totals["rewards_followed_by_subject_action"]) / rewards
                        if rewards else None
                    ),
                    "distinct_retries_per_episode": (
                        float(totals["distinct_retries_after_reward"]) / episodes_count
                    ),
                    "causal_progress_per_episode": (
                        float(totals["causal_progress_events"]) / episodes_count
                    ),
                    "tool_protocol_errors_per_episode": (
                        float(totals.get("tool_protocol_errors") or 0) / episodes_count
                    ),
                    "invalid_tool_parameter_errors_per_episode": (
                        float(totals.get("invalid_tool_parameter_errors") or 0)
                        / episodes_count
                    ),
                    "unparsed_tool_intents_per_episode": (
                        float(totals.get("unparsed_tool_intents") or 0)
                        / episodes_count
                    ),
                }
            return {
                "information_boundary": (
                    "GT-free training trajectories, Reward interactions, generalized "
                    "errors, and control-plane indexes; no ground truth or known PoC"
                ),
                "aggregate": index,
                "objective_summary_by_harness_version": objectives,
                "card_selection": {
                    "total_episodes": index["episode_count"],
                    "included_cards": len(episodes),
                    "policy": "recent plus category representatives",
                },
                "episodes": episodes,
                "error_history": [
                    {
                        "episode_id": card["episode_id"],
                        "harness_version": card["metrics"].get("harness_version"),
                        "kind": item.get("kind"),
                        "category": item.get("category"),
                        "confidence": item.get("confidence"),
                        "evidence_sequences": item.get("evidence_sequences", []),
                    }
                    for card in episodes
                    for item in card.get("experiences", [])
                ],
            }
