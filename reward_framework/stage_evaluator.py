"""Deterministic stage assessment for the reward loop."""

from __future__ import annotations

from .assertion_reward import RewardSpec
from .models import STAGES, RawRuntimeReport, StageAssessment, StageStatus


def _status_from_claims(results: list[dict], *, require_all: bool = False) -> StageStatus:
    if not results:
        return StageStatus.NOT_DECLARED
    matched = [item for item in results if item.get("matched_vulnerable_state") is True]
    reached = [item for item in results if item.get("reached") is True]
    evaluated = [item for item in results if item.get("evaluated") is True]
    if require_all:
        if matched and len(matched) == len(results):
            return StageStatus.CONFIRMED
        if reached and len(reached) == len(results) and len(evaluated) == len(results):
            return StageStatus.REFUTED
        if reached:
            return StageStatus.UNRESOLVED
        return StageStatus.NOT_REACHED
    if matched:
        return StageStatus.CONFIRMED
    if reached and evaluated and len(evaluated) == len(reached):
        return StageStatus.REFUTED
    if reached:
        return StageStatus.UNRESOLVED
    return StageStatus.NOT_REACHED


def evaluate_stages(spec: RewardSpec, report: RawRuntimeReport) -> StageAssessment:
    by_stage: dict[str, list[dict]] = {stage: [] for stage in STAGES}
    for item in report.claim_results:
        by_stage.setdefault(item.stage, []).append(item.to_dict())

    normalized: dict[str, StageStatus] = {}
    for stage in STAGES:
        declared = spec.stage_claims(stage)
        if not declared:
            normalized[stage] = StageStatus.NOT_DECLARED
            continue
        observed = report.stage_observations.get(stage)
        if observed is not None and (stage == "admission" or not by_stage[stage]):
            normalized[stage] = observed
            continue
        if observed is not None and observed == StageStatus.UNRESOLVED:
            normalized[stage] = StageStatus.UNRESOLVED
            continue
        if stage == "propagation":
            normalized[stage] = _status_from_claims(
                by_stage[stage], require_all=True
            )
        else:
            normalized[stage] = _status_from_claims(by_stage[stage])

    consistency = "consistent"
    conflict_stages: list[str] = []
    if report.trigger_observed:
        # The independent trigger oracle is a stronger signal than any
        # individual source-level probe.  If the target vulnerability signal is
        # observed while a declared stage probe says the vulnerable state was
        # absent or unreachable, the correct conclusion is that the Reward Spec
        # or its source-location mapping is incomplete.  It must not be reported
        # as a refutation of the candidate PoC.
        for stage in STAGES:
            if normalized[stage] in {
                StageStatus.NOT_DECLARED,
                StageStatus.CONFIRMED,
            }:
                continue
            normalized[stage] = StageStatus.SPEC_OR_MAPPING_CONFLICT
            conflict_stages.append(stage)
        if conflict_stages:
            consistency = "spec_or_mapping_conflict"

    prefix: list[str] = []
    first_unresolved: str | None = None
    gate_open = True
    for stage in STAGES:
        status = normalized[stage]
        if status == StageStatus.NOT_DECLARED:
            continue
        if gate_open and status == StageStatus.CONFIRMED:
            prefix.append(stage)
            continue
        if gate_open:
            first_unresolved = stage
            gate_open = False
            continue
        if status == StageStatus.CONFIRMED:
            normalized[stage] = StageStatus.OBSERVED_BUT_BLOCKED
        elif status in {StageStatus.SPEC_OR_MAPPING_CONFLICT}:
            continue
        elif status not in {StageStatus.REFUTED, StageStatus.UNRESOLVED}:
            normalized[stage] = StageStatus.NOT_REACHED

    return StageAssessment(
        normalized, tuple(prefix), first_unresolved,
        consistency, tuple(conflict_stages),
    )
