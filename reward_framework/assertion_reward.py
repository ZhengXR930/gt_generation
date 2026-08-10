"""Task-conditioned, GT-free assertion reward protocol.

The protocol deliberately has one admission channel and three semantic claim
kinds.  It reuses the meaning of the dataset assertions without importing any
GT artifact: required claims encode safety obligations, observed claims encode
unsafe runtime states, and transition claims encode ordered value/object flow.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


CLAIM_KINDS = {"required", "observed", "transition"}
OPS = {"eq", "ne", "lt", "le", "gt", "ge", "same_object"}
IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
LITERALS = {"true", "false", "null", "nullptr"}


@dataclass(frozen=True)
class ClaimLocation:
    file: str
    function: str
    line: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClaimLocation":
        if not isinstance(value, dict):
            raise ValueError("claim location must be an object")
        file = str(value.get("file") or "").strip()
        function = str(value.get("function") or "").strip()
        line = value.get("line")
        if not file or not function or not isinstance(line, int) or line < 1:
            raise ValueError("claim location requires file, function, and positive line")
        return cls(file, function, line)


@dataclass(frozen=True)
class ClaimCheck:
    op: str
    left: Any
    right: Any

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClaimCheck":
        if not isinstance(value, dict) or set(value) != {"op", "left", "right"}:
            raise ValueError("claim check must contain only op, left, and right")
        op = str(value["op"])
        if op not in OPS:
            raise ValueError(f"unsupported claim operator: {op}")
        for side in (value["left"], value["right"]):
            if isinstance(side, (dict, list)):
                raise ValueError("claim operands must be source expressions or literals")
        return cls(op, value["left"], value["right"])


@dataclass(frozen=True)
class AdmissionAssertion:
    assertion_id: str
    at: ClaimLocation

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.assertion_id, "at": asdict(self.at)}


@dataclass(frozen=True)
class SemanticAssertion:
    assertion_id: str
    kind: str
    at: ClaimLocation
    check: ClaimCheck
    source: ClaimLocation | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {
            "id": self.assertion_id,
            "kind": self.kind,
            "at": asdict(self.at),
            "check": asdict(self.check),
        }
        if self.source is not None:
            value["from"] = asdict(self.source)
        return value


@dataclass(frozen=True)
class AssertionRewardSpec:
    admission: tuple[AdmissionAssertion, ...]
    assertions: tuple[SemanticAssertion, ...]
    protocol: str = "assertion-reward-v1"

    @property
    def constructable(self) -> bool:
        return bool(self.admission or self.assertions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "admission": [item.to_dict() for item in self.admission],
            "claims": [item.to_dict() for item in self.assertions],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssertionRewardSpec":
        if not isinstance(value, dict):
            raise ValueError("assertion Reward Spec must be an object")
        if value.get("protocol", "assertion-reward-v1") != "assertion-reward-v1":
            raise ValueError("unsupported assertion Reward protocol")
        admission = []
        for index, raw in enumerate(value.get("admission") or [], 1):
            item = dict(raw)
            assertion_id = str(item.get("id") or f"admission_{index:02d}")
            admission.append(AdmissionAssertion(
                assertion_id, ClaimLocation.from_dict(item.get("at"))
            ))
        assertions = []
        for index, raw in enumerate(value.get("claims") or [], 1):
            item = dict(raw)
            kind = str(item.get("kind") or "")
            if kind not in CLAIM_KINDS:
                raise ValueError(f"invalid semantic claim kind: {kind}")
            source_raw = item.get("from")
            if kind == "transition" and source_raw is None:
                raise ValueError("transition claim requires from")
            if kind != "transition" and source_raw is not None:
                raise ValueError("from is allowed only for transition claims")
            assertions.append(SemanticAssertion(
                assertion_id=str(item.get("id") or f"{kind}_{index:02d}"),
                kind=kind,
                at=ClaimLocation.from_dict(item.get("at")),
                check=ClaimCheck.from_dict(item.get("check")),
                source=(ClaimLocation.from_dict(source_raw) if source_raw else None),
            ))
        if not admission:
            raise ValueError("Reward Spec requires at least one admission assertion")
        if not assertions:
            raise ValueError("Reward Spec requires at least one semantic claim")
        return cls(tuple(admission), tuple(assertions))


@dataclass(frozen=True)
class AssertionResult:
    assertion_id: str
    kind: str
    status: str
    reached: bool
    evaluated: bool
    check_satisfied: bool | None
    matched_vulnerable_state: bool | None
    left: Any = None
    right: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AssertionAssessment:
    admission: str
    claims: tuple[AssertionResult, ...]
    information_gain: int
    consistency: str = "consistent"

    # Compatibility properties for the existing crash-safe state store.  New
    # consumers use claim_results and information_gain, never this projection.
    @property
    def first_unresolved(self) -> str | None:
        if self.admission != "confirmed":
            return "admission"
        item = next((x for x in self.claims if not x.evaluated), None)
        return item.assertion_id if item else None

    @property
    def longest_confirmed_prefix(self) -> tuple[str, ...]:
        return tuple(
            item.assertion_id for item in self.claims
            if item.matched_vulnerable_state is True
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "assertion-reward-v1",
            "admission": self.admission,
            "claims": [item.to_dict() for item in self.claims],
            "information_gain": self.information_gain,
            "consistency": self.consistency,
            "matched_claims": list(self.longest_confirmed_prefix),
            "first_unresolved": self.first_unresolved,
        }


def validate_spec_sources(spec: AssertionRewardSpec, source_root: Path) -> None:
    root = source_root.resolve()
    file_text: dict[str, str] = {}
    locations = [item.at for item in spec.admission]
    for claim in spec.assertions:
        locations.append(claim.at)
        if claim.source:
            locations.append(claim.source)
    for location in locations:
        path = (root / location.file).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"claim path escapes source view: {location.file}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if location.line > len(lines):
            raise ValueError(f"claim line is outside source: {location.file}:{location.line}")
        text = "\n".join(lines)
        file_text[location.file] = text
        function = location.function.rsplit("::", 1)[-1]
        if location.function not in text and function not in text:
            raise ValueError(f"claim function absent from source: {location.function}")
    for claim in spec.assertions:
        operand_locations = (
            (claim.check.left, claim.source or claim.at),
            (claim.check.right, claim.at),
        )
        for operand, location in operand_locations:
            if not isinstance(operand, str):
                continue
            expression = operand.strip()
            assignment = bool(re.search(r"(?<![!<>=])=(?!=)", expression))
            function_call = bool(re.search(r"\b[A-Za-z_]\w*\s*\(", expression))
            if not expression or ";" in expression or assignment or function_call:
                raise ValueError(f"unsafe claim operand: {operand!r}")
            lowered = expression.lower()
            if lowered in LITERALS or re.fullmatch(r"[-+]?(0x[0-9a-fA-F]+|\d+)", expression):
                continue
            identifiers = {
                token for token in IDENTIFIER.findall(expression)
                if token not in {"sizeof"}
            }
            text = file_text.get(location.file, "")
            missing = [token for token in sorted(identifiers) if token not in text]
            if missing:
                raise ValueError(
                    f"claim operand is not source-visible at {location.file}: "
                    f"{operand!r}; missing={missing[:3]}"
                )


def check_value(op: str, left: Any, right: Any) -> bool:
    if op in {"eq", "same_object"}:
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    raise ValueError(f"unsupported claim operator: {op}")


def assess_assertions(
    *, admission: str, results: tuple[AssertionResult, ...],
    previous: dict[str, Any] | None, trigger_observed: bool,
) -> AssertionAssessment:
    old = {
        str(item.get("assertion_id")): item
        for item in ((previous or {}).get("assessment") or {}).get("claims", [])
        if isinstance(item, dict)
    }
    previous_assessment = (previous or {}).get("assessment") or {}
    gain = int(
        admission == "confirmed"
        and previous_assessment.get("admission") != "confirmed"
    )
    for item in results:
        before = old.get(item.assertion_id)
        if item.evaluated and (
            before is None
            or not before.get("evaluated")
            or before.get("matched_vulnerable_state") != item.matched_vulnerable_state
        ):
            gain += 1
    conflict = (
        trigger_observed
        and admission == "confirmed"
        and bool(results)
        and all(item.evaluated for item in results)
        and all(item.matched_vulnerable_state is False for item in results)
    )
    return AssertionAssessment(
        admission, results, gain,
        "spec_or_mapping_conflict" if conflict else "consistent",
    )
