"""Crash-safe task, trajectory, candidate, and evidence persistence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    EvidenceRecord,
    EvidenceState,
    HarnessPolicy,
    HarnessState,
    ObservationState,
    TaskContext,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class RegisteredCandidate:
    candidate_id: str
    sha256: str
    attempt_number: int
    duplicate_of: str | None
    candidate_dir: Path
    attempt_dir: Path


class StateStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.candidates_dir = self.root / "candidates"
        self.evidence_dir = self.root / "evidence"

    @property
    def task_path(self) -> Path:
        return self.root / "task_context.json"

    @property
    def observation_path(self) -> Path:
        """Legacy mirror retained for existing checkpoints and analysis code."""
        return self.root / "observation_state.json"

    @property
    def trajectory_path(self) -> Path:
        return self.root / "trajectory_state.json"

    @property
    def evidence_state_path(self) -> Path:
        return self.root / "evidence_state.json"

    @property
    def harness_state_path(self) -> Path:
        return self.root / "harness_state.json"

    def save_task(self, task: TaskContext) -> None:
        if self.task_path.exists():
            existing = TaskContext.from_dict(json.loads(self.task_path.read_text()))
            if existing.task_id != task.task_id:
                raise ValueError("state directory already belongs to another task")
        atomic_json(self.task_path, task.to_dict())

    def load_task(self) -> TaskContext:
        return TaskContext.from_dict(json.loads(self.task_path.read_text()))

    def save_observation(self, state: ObservationState) -> None:
        atomic_json(self.trajectory_path, state.to_dict())
        # Version-one consumers expect this exact path. Keep it as a mirror,
        # not as a second independently mutable state.
        atomic_json(self.observation_path, state.to_dict())

    def load_observation(self) -> ObservationState:
        path = (
            self.trajectory_path
            if self.trajectory_path.exists()
            else self.observation_path
        )
        if not path.exists():
            return ObservationState()
        return ObservationState.from_dict(json.loads(path.read_text()))

    def save_evidence_state(self, state: EvidenceState) -> None:
        atomic_json(self.evidence_state_path, state.to_dict())

    def load_evidence_state(self) -> EvidenceState:
        if not self.evidence_state_path.exists():
            rebuilt = EvidenceState()
            if self.evidence_dir.exists():
                for path in sorted(self.evidence_dir.glob("attempt_*.json")):
                    value = json.loads(path.read_text())
                    runtime = value.get("runtime") or {}
                    assessment = value.get("assessment") or {}
                    feedback = value.get("feedback") or {}
                    errors: list[str] = []
                    if value.get("duplicate_of"):
                        errors.append("duplicate_candidate")
                    if runtime.get("instrumentation_available") is False:
                        errors.append("instrumentation_unavailable")
                    if runtime.get("error"):
                        errors.append("runtime_or_probe_error")
                    boundary = assessment.get("first_unresolved")
                    if boundary:
                        errors.append(f"causal_boundary:{boundary}")
                    if feedback.get("contradiction"):
                        errors.append("candidate_claim_refuted")
                    for error in errors:
                        rebuilt.recurring_errors[error] = (
                            rebuilt.recurring_errors.get(error, 0) + 1
                        )
                    summary = {
                        "attempt_number": value.get("attempt_number"),
                        "candidate_id": value.get("candidate_id"),
                        "candidate_sha256": value.get("candidate_sha256"),
                        "duplicate_of": value.get("duplicate_of"),
                        "trigger_observed": runtime.get("trigger_observed") is True,
                        "instrumentation_available": runtime.get(
                            "instrumentation_available"
                        ),
                        "runtime_facts": list(runtime.get("facts") or []),
                        "assessment": assessment,
                        "feedback": feedback,
                        "errors": errors,
                        "created_at": value.get("created_at"),
                    }
                    rebuilt.attempts.append(summary)
                    rebuilt.latest_attempt_number = int(
                        value.get("attempt_number") or 0
                    )
                    rebuilt.latest_candidate_id = value.get("candidate_id")
                    rebuilt.last_confirmed_prefix = tuple(
                        assessment.get("longest_confirmed_prefix") or []
                    )
            if rebuilt.attempts:
                self.save_evidence_state(rebuilt)
            return rebuilt
        return EvidenceState.from_dict(json.loads(self.evidence_state_path.read_text()))

    def initialize_harness(
        self, *, platform: str, baseline_profile: str, max_iterations: int = 100
    ) -> HarnessState:
        if self.harness_state_path.exists():
            return self.load_harness_state()
        state = HarnessState(
            baseline_profile=baseline_profile,
            current_policy=HarnessPolicy(
                platform=platform, max_iterations=max_iterations
            ),
        )
        self.save_harness_state(state)
        return state

    def save_harness_state(self, state: HarnessState) -> None:
        atomic_json(self.harness_state_path, state.to_dict())

    def load_harness_state(self) -> HarnessState:
        return HarnessState.from_dict(json.loads(self.harness_state_path.read_text()))

    def global_state(self) -> dict[str, Any]:
        """One complete optimizer view without lossy boolean summaries."""
        return {
            "task": self.load_task().to_dict(),
            "trajectory": self.load_observation().to_dict(),
            "evidence": self.load_evidence_state().to_dict(),
            "harness": self.load_harness_state().to_dict(),
        }

    def append_event(self, *, source: str, kind: str,
                     payload: dict[str, Any]) -> ObservationState:
        state = self.load_observation()
        state.append(timestamp=utc_now(), source=source, kind=kind, payload=payload)
        self.save_observation(state)
        return state

    def _index(self) -> dict[str, Any]:
        path = self.candidates_dir / "index.json"
        if not path.exists():
            return {"total_submissions": 0, "unique_candidates": 0,
                    "by_sha256": {}, "attempts": []}
        return json.loads(path.read_text())

    def register_candidate(self, *, poc_path: Path, trace_path: Path,
                           checkpoint_path: Path | None = None) -> RegisteredCandidate:
        poc = poc_path.resolve()
        trace = trace_path.resolve()
        if not poc.is_file() or not trace.is_file():
            raise FileNotFoundError("candidate PoC and trace must both exist")
        content = poc.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        index = self._index()
        attempt_number = int(index["total_submissions"]) + 1
        duplicate_of = index["by_sha256"].get(digest)
        if duplicate_of:
            candidate_id = str(duplicate_of)
            candidate_dir = self.candidates_dir / candidate_id
        else:
            unique_number = int(index["unique_candidates"]) + 1
            candidate_id = f"candidate_{unique_number:04d}_{digest[:12]}"
            candidate_dir = self.candidates_dir / candidate_id
            candidate_dir.mkdir(parents=True, exist_ok=True)
            (candidate_dir / "poc").write_bytes(content)
            index["by_sha256"][digest] = candidate_id
            index["unique_candidates"] = unique_number

        attempt_dir = candidate_dir / "attempts" / f"attempt_{attempt_number:04d}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(trace, attempt_dir / "trace.json")
        shutil.copy2(trace, candidate_dir / "latest_trace.json")
        checkpoint_reference = None
        if checkpoint_path is not None:
            checkpoint_reference = str(checkpoint_path.resolve())
        metadata = {
            "attempt_number": attempt_number,
            "candidate_id": candidate_id,
            "sha256": digest,
            "duplicate_of": duplicate_of,
            "source_poc": str(poc),
            "source_trace": str(trace),
            "checkpoint": checkpoint_reference,
            "created_at": utc_now(),
        }
        atomic_json(attempt_dir / "submission.json", metadata)
        index["total_submissions"] = attempt_number
        index["attempts"].append(metadata)
        atomic_json(self.candidates_dir / "index.json", index)
        return RegisteredCandidate(
            candidate_id, digest, attempt_number, duplicate_of,
            candidate_dir, attempt_dir,
        )

    def save_evidence(self, record: EvidenceRecord) -> Path:
        path = self.evidence_dir / f"attempt_{record.attempt_number:04d}.json"
        atomic_json(path, record.to_dict())
        atomic_json(
            self.candidates_dir / record.candidate_id / "latest_evidence.json",
            record.to_dict(),
        )
        aggregate = self.load_evidence_state()
        aggregate.append_record(record)
        self.save_evidence_state(aggregate)
        return path

    def previous_distinct_evidence(self, candidate_sha256: str) -> dict[str, Any] | None:
        if not self.evidence_dir.exists():
            return None
        for path in sorted(self.evidence_dir.glob("attempt_*.json"), reverse=True):
            value = json.loads(path.read_text())
            if value.get("candidate_sha256") != candidate_sha256:
                return value
        return None

    def candidate_stats(self) -> dict[str, Any]:
        index = self._index()
        total = int(index["total_submissions"])
        unique = int(index["unique_candidates"])
        return {
            "total_submissions": total,
            "unique_candidates": unique,
            "duplicate_submissions": total - unique,
            "unique_ratio": (unique / total) if total else 0.0,
        }

    def latest_candidate_sha256(self) -> str | None:
        """Return the last candidate identity recorded at the submission boundary."""
        attempts = self._index().get("attempts") or []
        if not attempts:
            return None
        value = str(attempts[-1].get("sha256") or "").strip()
        return value or None
