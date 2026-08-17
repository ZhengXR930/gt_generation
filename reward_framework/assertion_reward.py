"""GT-free stage Reward Spec and deterministic claim assessment.

The Reward Spec is intentionally aligned with the offline evaluation axes but
is generated only from the public issue description and the vulnerable source
tree:

``admission -> source -> root -> propagation -> sink``.

It does not import GT invariants, sanitizer traces, known PoCs, or historical
crash states.  Runtime feedback is based on passive observations of the
submitted candidate and on the claim polarity defined here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


STAGES = ("admission", "source", "root", "propagation", "sink")
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
    def from_dict(cls, value: dict[str, Any] | None) -> "ClaimCheck | None":
        if value is None:
            return None
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
class RewardClaim:
    claim_id: str
    stage: str
    at: ClaimLocation
    claim: str
    check: ClaimCheck | None = None
    source: ClaimLocation | None = None
    operands: tuple[str, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"invalid claim stage: {self.stage}")
        if not self.claim_id.strip() or not self.claim.strip():
            raise ValueError("claim id and claim text are required")
        if self.stage == "propagation" and self.source is None:
            raise ValueError("propagation claim requires a source endpoint")
        if self.stage != "propagation" and self.source is not None:
            raise ValueError("source endpoint is only valid for propagation")
        if self.stage in {"root", "sink"} and self.check is None:
            raise ValueError(f"{self.stage} claim requires a vulnerable-state check")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.claim_id,
            "at": asdict(self.at),
            "claim": self.claim,
        }
        if self.check is not None:
            value["check"] = asdict(self.check)
        if self.operands and self.stage != "propagation":
            value["operands"] = list(self.operands)
        if self.stage == "propagation":
            value["from"] = asdict(self.source)
            value["to"] = value.pop("at")
            value["via"] = list(self.operands)
        return value


@dataclass(frozen=True)
class RewardSpec:
    admission: tuple[RewardClaim, ...]
    source: tuple[RewardClaim, ...]
    root: tuple[RewardClaim, ...]
    propagation_required: tuple[RewardClaim, ...]
    propagation_optional: tuple[RewardClaim, ...]
    sink: tuple[RewardClaim, ...]

    @property
    def constructable(self) -> bool:
        return bool(self.admission and (self.source or self.root or self.sink))

    @property
    def all_claims(self) -> tuple[RewardClaim, ...]:
        return (
            self.admission + self.source + self.root
            + self.propagation_required + self.propagation_optional + self.sink
        )

    def stage_claims(self, stage: str) -> tuple[RewardClaim, ...]:
        if stage == "admission":
            return self.admission
        if stage == "source":
            return self.source
        if stage == "root":
            return self.root
        if stage == "propagation":
            return self.propagation_required
        if stage == "sink":
            return self.sink
        raise ValueError(f"invalid stage: {stage}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "admission": [item.to_dict() for item in self.admission],
            "source": [item.to_dict() for item in self.source],
            "root": [item.to_dict() for item in self.root],
            "propagation": {
                "required": [item.to_dict() for item in self.propagation_required],
                "optional": [item.to_dict() for item in self.propagation_optional],
            },
            "sink": [item.to_dict() for item in self.sink],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RewardSpec":
        if not isinstance(value, dict) or set(value) != {
            "admission", "source", "root", "propagation", "sink",
        }:
            raise ValueError(
                "Reward Spec must contain exactly admission/source/root/"
                "propagation/sink"
            )
        propagation = value["propagation"]
        if not isinstance(propagation, dict) or set(propagation) != {
            "required", "optional",
        }:
            raise ValueError("propagation must contain required and optional arrays")
        admission = tuple(
            _claim_from_dict(stage="admission", raw=raw, index=index)
            for index, raw in enumerate(_items(value["admission"], "admission"), 1)
        )
        source = tuple(
            _claim_from_dict(stage="source", raw=raw, index=index)
            for index, raw in enumerate(_items(value["source"], "source"), 1)
        )
        root = tuple(
            _claim_from_dict(stage="root", raw=raw, index=index)
            for index, raw in enumerate(_items(value["root"], "root"), 1)
        )
        prop_required = tuple(
            _claim_from_dict(
                stage="propagation", raw=raw, index=index, required=True
            )
            for index, raw in enumerate(
                _items(propagation["required"], "propagation.required"), 1
            )
        )
        prop_optional = tuple(
            _claim_from_dict(
                stage="propagation", raw=raw, index=index, required=False
            )
            for index, raw in enumerate(
                _items(propagation["optional"], "propagation.optional"), 1
            )
        )
        sink = tuple(
            _claim_from_dict(stage="sink", raw=raw, index=index)
            for index, raw in enumerate(_items(value["sink"], "sink"), 1)
        )
        spec = cls(admission, source, root, prop_required, prop_optional, sink)
        if not admission:
            raise ValueError("Reward Spec requires at least one admission claim")
        if not (source or root or sink):
            raise ValueError("Reward Spec requires at least one semantic claim")
        return spec


def _items(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    if len(value) > 6:
        raise ValueError(f"{field} contains too many claims")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{field} items must be objects")
    return list(value)


def _claim_from_dict(
    *, stage: str, raw: dict[str, Any], index: int, required: bool = True,
) -> RewardClaim:
    claim_id = str(raw.get("id") or f"{stage}_{index:02d}").strip()
    claim = str(raw.get("claim") or raw.get("description") or "").strip()
    if stage == "propagation":
        source = ClaimLocation.from_dict(raw.get("from"))
        at = ClaimLocation.from_dict(raw.get("to") or raw.get("at"))
        operands = tuple(str(x).strip() for x in raw.get("via") or raw.get("operands") or [])
    else:
        source = None
        at = ClaimLocation.from_dict(raw.get("at"))
        operands = tuple(str(x).strip() for x in raw.get("operands") or [])
    operands = tuple(x for x in operands if x)
    return RewardClaim(
        claim_id=claim_id,
        stage=stage,
        at=at,
        claim=claim,
        check=ClaimCheck.from_dict(raw.get("check")),
        source=source,
        operands=operands,
        required=required,
    )


@dataclass(frozen=True)
class ClaimResult:
    claim_id: str
    stage: str
    status: str
    reached: bool
    evaluated: bool
    check_satisfied: bool | None
    matched_vulnerable_state: bool | None
    required: bool = True
    left: Any = None
    right: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_spec_sources(spec: RewardSpec, source_root: Path) -> None:
    root = source_root.resolve()
    file_text: dict[str, str] = {}
    locations: list[ClaimLocation] = []
    for claim in spec.all_claims:
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
    for claim in spec.all_claims:
        _validate_expressions(
            [claim.check.left, claim.check.right] if claim.check else claim.operands,
            claim.at,
            file_text,
        )
        if claim.stage == "propagation" and claim.source and claim.check:
            _validate_expressions([claim.check.left], claim.source, file_text)


def _validate_expressions(
    expressions: Iterable[Any], location: ClaimLocation, file_text: dict[str, str],
) -> None:
    for operand in expressions:
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
        member_identifiers = {
            match.group(1)
            for match in re.finditer(
                r"(?:->|\.)\s*([A-Za-z_][A-Za-z0-9_]*)", expression
            )
        }
        identifiers = {
            token for token in IDENTIFIER.findall(expression)
            if token not in {"sizeof"} and token not in member_identifiers
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
