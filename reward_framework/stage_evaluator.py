"""Deterministic five-stage gate; the Reward Agent cannot change it."""

from __future__ import annotations

from .models import (
    STAGES,
    RawRuntimeReport,
    RewardSpec,
    StageAssessment,
    StageStatus,
)


def evaluate_stages(spec: RewardSpec, report: RawRuntimeReport) -> StageAssessment:
    normalized: dict[str, StageStatus] = {}
    prefix: list[str] = []
    gate_open = True
    first_unresolved: str | None = None

    for stage in STAGES:
        if spec.claims[stage] is None:
            normalized[stage] = StageStatus.NOT_DECLARED
            continue
        observed = report.stage_observations.get(stage)
        if observed is None:
            # Missing or unavailable instrumentation is absence of evidence,
            # never proof that the stage was not reached.
            observed = StageStatus.UNRESOLVED
        if gate_open and observed == StageStatus.CONFIRMED:
            normalized[stage] = StageStatus.CONFIRMED
            prefix.append(stage)
            continue
        if gate_open:
            normalized[stage] = observed
            gate_open = False
            if first_unresolved is None:
                first_unresolved = stage
            continue
        if observed == StageStatus.CONFIRMED:
            normalized[stage] = StageStatus.OBSERVED_BUT_BLOCKED
        elif observed == StageStatus.REFUTED:
            normalized[stage] = StageStatus.REFUTED
        else:
            normalized[stage] = StageStatus.NOT_REACHED

    return StageAssessment(normalized, tuple(prefix), first_unresolved)
