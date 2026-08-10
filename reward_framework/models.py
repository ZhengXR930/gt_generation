"""Small, JSON-serializable domain model owned by the controller."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .assertion_reward import AssertionRewardSpec


STAGES = ("admission", "source", "root", "propagation", "target")


class StageStatus(str, Enum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNRESOLVED = "unresolved"
    NOT_REACHED = "not_reached"
    NOT_DECLARED = "not_declared"
    OBSERVED_BUT_BLOCKED = "observed_but_blocked"


@dataclass(frozen=True)
class SourceAnchor:
    file: str
    function: str
    fact: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceAnchor":
        if set(value) != {"file", "function", "fact"}:
            raise ValueError("source anchor must contain file, function, and fact")
        fields = {key: str(value[key]).strip() for key in value}
        if not all(fields.values()):
            raise ValueError("source anchor fields cannot be empty")
        return cls(**fields)


@dataclass(frozen=True)
class RewardSpec:
    claims: dict[str, str | None]
    evidence: dict[str, tuple[SourceAnchor, ...]]

    def __post_init__(self) -> None:
        if tuple(self.claims) != STAGES or set(self.evidence) != set(STAGES):
            raise ValueError(f"reward spec stages must be {STAGES}")
        for stage in STAGES:
            claim = self.claims[stage]
            if claim is not None and not str(claim).strip():
                raise ValueError(f"{stage} claim cannot be blank")
            anchors = self.evidence[stage]
            if len(anchors) > 2:
                raise ValueError(f"{stage} has more than two source anchors")
            if bool(claim) != bool(anchors):
                raise ValueError(f"{stage} claim and evidence must co-occur")

    @property
    def constructable(self) -> bool:
        return any(self.claims.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims": dict(self.claims),
            "evidence": {
                stage: [asdict(anchor) for anchor in self.evidence[stage]]
                for stage in STAGES
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RewardSpec":
        if set(value) != {"claims", "evidence"}:
            raise ValueError("reward spec must contain claims and evidence")
        claims = value["claims"]
        evidence = value["evidence"]
        if not isinstance(claims, dict) or not isinstance(evidence, dict):
            raise ValueError("claims and evidence must be objects")
        normalized_claims: dict[str, str | None] = {}
        normalized_evidence: dict[str, tuple[SourceAnchor, ...]] = {}
        for stage in STAGES:
            claim = claims.get(stage)
            normalized_claims[stage] = None if claim is None else str(claim).strip()
            items = evidence.get(stage)
            if not isinstance(items, list):
                raise ValueError(f"{stage} evidence must be a list")
            normalized_evidence[stage] = tuple(SourceAnchor.from_dict(x) for x in items)
        return cls(normalized_claims, normalized_evidence)


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    issue_description: str
    codebase_root: str
    source_manifest_sha256: str
    reward_spec: RewardSpec | AssertionRewardSpec
    spec_model: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reward_spec"] = self.reward_spec.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskContext":
        copied = dict(value)
        raw_spec = copied["reward_spec"]
        copied["reward_spec"] = (
            AssertionRewardSpec.from_dict(raw_spec)
            if raw_spec.get("protocol") == "assertion-reward-v1"
            else RewardSpec.from_dict(raw_spec)
        )
        return cls(**copied)


@dataclass(frozen=True)
class TrajectoryEvent:
    sequence: int
    timestamp: str
    source: str
    kind: str
    payload: dict[str, Any]


@dataclass
class TrajectoryState:
    events: list[TrajectoryEvent] = field(default_factory=list)
    last_observer_sequence: int = 0
    last_submission_sequence: int = 0
    submission_requested: bool = False
    materialization_outstanding: bool = False
    awaiting_verification: bool = False
    terminal_reason: str | None = None

    def append(self, *, timestamp: str, source: str, kind: str,
               payload: dict[str, Any]) -> TrajectoryEvent:
        event = TrajectoryEvent(
            sequence=len(self.events) + 1,
            timestamp=timestamp,
            source=source,
            kind=kind,
            payload=payload,
        )
        self.events.append(event)
        return event

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [asdict(event) for event in self.events],
            "last_observer_sequence": self.last_observer_sequence,
            "last_submission_sequence": self.last_submission_sequence,
            "submission_requested": self.submission_requested,
            "materialization_outstanding": self.materialization_outstanding,
            "awaiting_verification": self.awaiting_verification,
            "terminal_reason": self.terminal_reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TrajectoryState":
        copied = dict(value)
        copied["events"] = [TrajectoryEvent(**item) for item in copied.get("events", [])]
        return cls(**copied)


# Backward-compatible public name used by the first Reward Framework version.
# New code and persisted files call this the trajectory state because it is the
# platform-neutral, complete action/observation history rather than a summary.
ObservationState = TrajectoryState


@dataclass
class EvidenceState:
    """Cross-candidate runtime memory used for diagnosis and error summaries."""

    attempts: list[dict[str, Any]] = field(default_factory=list)
    latest_attempt_number: int = 0
    latest_candidate_id: str | None = None
    last_confirmed_prefix: tuple[str, ...] = ()
    recurring_errors: dict[str, int] = field(default_factory=dict)

    def append_record(self, record: "EvidenceRecord") -> None:
        boundary = record.assessment.first_unresolved
        errors: list[str] = []
        if record.duplicate_of:
            errors.append("duplicate_candidate")
        if not record.runtime.instrumentation_available:
            errors.append("instrumentation_unavailable")
        if record.runtime.error:
            errors.append("runtime_or_probe_error")
        if boundary:
            errors.append(f"causal_boundary:{boundary}")
        if record.feedback.contradiction:
            errors.append("candidate_claim_refuted")
        for error in errors:
            self.recurring_errors[error] = self.recurring_errors.get(error, 0) + 1
        self.attempts.append({
            "attempt_number": record.attempt_number,
            "candidate_id": record.candidate_id,
            "candidate_sha256": record.candidate_sha256,
            "duplicate_of": record.duplicate_of,
            "trigger_observed": record.runtime.trigger_observed,
            "instrumentation_available": record.runtime.instrumentation_available,
            "runtime_facts": [asdict(fact) for fact in record.runtime.facts],
            "assessment": record.assessment.to_dict(),
            "feedback": record.feedback.to_dict(),
            "errors": errors,
            "created_at": record.created_at,
        })
        self.latest_attempt_number = record.attempt_number
        self.latest_candidate_id = record.candidate_id
        self.last_confirmed_prefix = record.assessment.longest_confirmed_prefix

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "latest_attempt_number": self.latest_attempt_number,
            "latest_candidate_id": self.latest_candidate_id,
            "last_confirmed_prefix": list(self.last_confirmed_prefix),
            "recurring_errors": dict(self.recurring_errors),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceState":
        copied = dict(value)
        copied["attempts"] = list(copied.get("attempts") or [])
        copied["last_confirmed_prefix"] = tuple(
            copied.get("last_confirmed_prefix") or []
        )
        copied["recurring_errors"] = dict(copied.get("recurring_errors") or {})
        return cls(**copied)


@dataclass(frozen=True)
class HarnessPolicy:
    """Episode-frozen launch constraints; never optimized inside a sample."""

    version: int = 1
    platform: str = "generic"
    max_iterations: int = 100

    def __post_init__(self) -> None:
        if self.version < 1 or self.max_iterations < 1:
            raise ValueError("harness policy version and max_iterations must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "HarnessPolicy":
        return cls(**{
            key: value[key] for key in ("version", "platform", "max_iterations")
            if key in value
        })


@dataclass
class HarnessState:
    baseline_profile: str
    current_policy: HarnessPolicy
    # This is the immutable fork version loaded when the episode process began.
    active_program_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_profile": self.baseline_profile,
            "current_policy": self.current_policy.to_dict(),
            "active_program_version": self.active_program_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "HarnessState":
        copied = dict(value)
        copied["current_policy"] = HarnessPolicy.from_dict(
            copied["current_policy"]
        )
        for legacy in (
            "revisions", "last_optimizer_sequence", "pending_reward_sequence",
            "pending_reward_attempt", "last_patcher_reward_sequence",
            "optimizer_errors",
        ):
            copied.pop(legacy, None)
        return cls(**copied)


@dataclass(frozen=True)
class Probe:
    stage: str
    anchor_kind: str
    file: str
    function: str
    statement: str | None
    captures: tuple[str, ...] = ()
    condition: str | None = None
    purpose: str = ""
    line: int | None = None
    claim_id: str | None = None
    claim_kind: str | None = None
    endpoint: str = "at"
    check_op: str | None = None
    left_operand: Any = None
    right_operand: Any = None

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"invalid probe stage: {self.stage}")
        if self.anchor_kind not in {"issue", "trace"}:
            raise ValueError("probe anchor_kind must be issue or trace")
        if not self.file.strip() or not self.function.strip():
            raise ValueError("probe file and function are required")


@dataclass(frozen=True)
class ProbePlan:
    probes: tuple[Probe, ...]
    trace_claims: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "probes": [asdict(probe) for probe in self.probes],
            "trace_claims": list(self.trace_claims),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProbePlan":
        probes = []
        for item in value.get("probes", []):
            copied = dict(item)
            copied["captures"] = tuple(copied.get("captures", []))
            probes.append(Probe(**copied))
        claims = value.get("trace_claims", [])
        if not isinstance(claims, list):
            raise ValueError("trace_claims must be a list")
        return cls(tuple(probes), tuple(str(x) for x in claims))


@dataclass(frozen=True)
class RuntimeFact:
    fact_id: str
    stage: str
    kind: str
    statement: str
    data: dict[str, Any] = field(default_factory=dict)
    trusted: bool = True

    def __post_init__(self) -> None:
        if self.stage not in STAGES and self.stage != "trigger":
            raise ValueError(f"invalid runtime fact stage: {self.stage}")
        if not self.fact_id or not self.statement:
            raise ValueError("runtime fact id and statement are required")


@dataclass(frozen=True)
class RawRuntimeReport:
    exit_code: int | None
    stdout: str
    stderr: str
    trigger_observed: bool
    stage_observations: dict[str, StageStatus]
    facts: tuple[RuntimeFact, ...]
    instrumentation_available: bool
    error: str | None = None
    claim_results: tuple[Any, ...] = ()


@dataclass(frozen=True)
class StageAssessment:
    stages: dict[str, StageStatus]
    longest_confirmed_prefix: tuple[str, ...]
    first_unresolved: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stages": {stage: self.stages[stage].value for stage in STAGES},
            "longest_confirmed_prefix": list(self.longest_confirmed_prefix),
            "first_unresolved": self.first_unresolved,
        }


@dataclass(frozen=True)
class Feedback:
    summary: str
    contradiction: str | None
    delta: str
    evidence_ids: tuple[str, ...]
    assessment: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "contradiction": self.contradiction,
            "delta": self.delta,
            "evidence_ids": list(self.evidence_ids),
            "assessment": self.assessment.to_dict(),
        }


@dataclass(frozen=True)
class EvidenceRecord:
    candidate_id: str
    candidate_sha256: str
    attempt_number: int
    duplicate_of: str | None
    probe_plan: ProbePlan
    runtime: RawRuntimeReport
    assessment: Any
    feedback: Feedback
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_sha256": self.candidate_sha256,
            "attempt_number": self.attempt_number,
            "duplicate_of": self.duplicate_of,
            "probe_plan": self.probe_plan.to_dict(),
            "runtime": {
                **asdict(self.runtime),
                "stage_observations": {
                    key: value.value
                    for key, value in self.runtime.stage_observations.items()
                },
                "facts": [asdict(fact) for fact in self.runtime.facts],
            },
            "assessment": self.assessment.to_dict(),
            "feedback": self.feedback.to_dict(),
            "created_at": self.created_at,
        }
