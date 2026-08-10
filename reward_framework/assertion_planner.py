"""Deterministically compile an assertion Reward Spec into passive probes."""

from __future__ import annotations

import re
from typing import Any

from .assertion_reward import AssertionRewardSpec
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


def plan_assertions(spec: AssertionRewardSpec) -> ProbePlan:
    probes: list[Probe] = []
    for item in spec.admission:
        probes.append(Probe(
            stage="admission", anchor_kind="issue",
            file=item.at.file, function=item.at.function, line=item.at.line,
            statement=None, purpose="Observe candidate input admission.",
            claim_id=item.assertion_id, claim_kind="admission",
        ))
    for claim in spec.assertions:
        left_literal, _ = _literal(claim.check.left)
        right_literal, _ = _literal(claim.check.right)
        if claim.kind == "transition":
            assert claim.source is not None
            probes.append(Probe(
                stage="propagation", anchor_kind="issue",
                file=claim.source.file, function=claim.source.function,
                line=claim.source.line, statement=None,
                captures=(() if left_literal else (str(claim.check.left),)),
                purpose="Capture the transition producer operand.",
                claim_id=claim.assertion_id, claim_kind=claim.kind,
                endpoint="from", check_op=claim.check.op,
                left_operand=claim.check.left, right_operand=claim.check.right,
            ))
            captures = () if right_literal else (str(claim.check.right),)
            probes.append(Probe(
                stage="propagation", anchor_kind="issue",
                file=claim.at.file, function=claim.at.function,
                line=claim.at.line, statement=None, captures=captures,
                purpose="Capture the transition consumer operand.",
                claim_id=claim.assertion_id, claim_kind=claim.kind,
                endpoint="at", check_op=claim.check.op,
                left_operand=claim.check.left, right_operand=claim.check.right,
            ))
            continue
        captures = tuple(
            str(value) for value, literal in (
                (claim.check.left, left_literal),
                (claim.check.right, right_literal),
            ) if not literal
        )
        probes.append(Probe(
            stage="root" if claim.kind == "required" else "target",
            anchor_kind="issue", file=claim.at.file,
            function=claim.at.function, line=claim.at.line,
            statement=None, captures=captures,
            purpose=f"Evaluate the {claim.kind} semantic assertion.",
            claim_id=claim.assertion_id, claim_kind=claim.kind,
            endpoint="at", check_op=claim.check.op,
            left_operand=claim.check.left, right_operand=claim.check.right,
        ))
    return ProbePlan(tuple(probes), ())
