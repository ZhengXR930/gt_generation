"""Compile a stage Reward Spec into passive runtime probes."""

from __future__ import annotations

import re
from typing import Any

from .assertion_reward import RewardClaim, RewardSpec
from .models import Probe, ProbePlan


def _literal(value: Any) -> tuple[bool, Any]:
    if not isinstance(value, str):
        return True, value
    text = value.strip()
    if text.lower() in {"true", "false"}:
        return True, text.lower() == "true"
    if text.lower() in {"null", "none", "nullptr"}:
        return True, None
    if re.fullmatch(r"[-+]?\d+", text):
        return True, int(text)
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
        return True, int(text, 16)
    return False, None


def _captures_for_claim(claim: RewardClaim, *, endpoint: str = "at") -> tuple[str, ...]:
    if claim.check is not None:
        operands = (
            (claim.check.left,) if endpoint == "from"
            else (claim.check.right,) if endpoint == "to"
            else (claim.check.left, claim.check.right)
        )
        return tuple(str(value) for value in operands if not _literal(value)[0])
    return tuple(claim.operands)


def _probe(
    claim: RewardClaim, *, endpoint: str = "at", file: str | None = None,
    function: str | None = None, line: int | None = None,
) -> Probe:
    return Probe(
        stage=claim.stage,
        anchor_kind="issue",
        file=file or claim.at.file,
        function=function or claim.at.function,
        line=line or claim.at.line,
        statement=None,
        captures=_captures_for_claim(claim, endpoint=endpoint),
        purpose=f"Observe Reward Spec {claim.stage} claim {claim.claim_id}.",
        claim_id=claim.claim_id,
        claim_kind=claim.stage,
        endpoint=endpoint,
        check_op=claim.check.op if claim.check else None,
        left_operand=claim.check.left if claim.check else None,
        right_operand=claim.check.right if claim.check else None,
        required=claim.required,
    )


def plan_assertions(spec: RewardSpec) -> ProbePlan:
    probes: list[Probe] = []
    for claim in spec.admission + spec.source + spec.root:
        probes.append(_probe(claim))
    for claim in spec.propagation_required + spec.propagation_optional:
        assert claim.source is not None
        probes.append(_probe(
            claim, endpoint="from", file=claim.source.file,
            function=claim.source.function, line=claim.source.line,
        ))
        probes.append(_probe(claim, endpoint="to"))
    for claim in spec.sink:
        probes.append(_probe(claim))
    return ProbePlan(tuple(probes), ())
