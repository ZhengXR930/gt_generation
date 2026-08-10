"""Parsing and validation for subject-authored semantic assertion claims."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


KINDS = {"required", "observed", "transition"}
OPS = {"eq", "ne", "lt", "le", "gt", "ge", "same_object"}


def parse_semantic_claims(response: str) -> list[dict[str, Any]] | None:
    try:
        value = json.loads((response or "").strip())
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return value


def _location_error(value: Any, label: str) -> str | None:
    if not isinstance(value, dict):
        return f"{label} must be an object"
    for field in ("file", "function"):
        if not str(value.get(field) or "").strip():
            return f"{label}.{field} must be a non-empty string"
    if not isinstance(value.get("line"), int):
        return f"{label}.line must be an integer"
    return None


def validate_semantic_claims(response: str) -> str | None:
    claims = parse_semantic_claims(response)
    if claims is None:
        return "the final answer must be one bare JSON array of claim objects"
    if not claims:
        return "the semantic claim array must not be empty"
    for index, claim in enumerate(claims, 1):
        prefix = f"claim {index}"
        kind = claim.get("kind")
        if kind not in KINDS:
            return f"{prefix}.kind must be required, observed, or transition"
        error = _location_error(claim.get("at"), f"{prefix}.at")
        if error:
            return error
        if kind == "transition":
            error = _location_error(claim.get("from"), f"{prefix}.from")
            if error:
                return error
        elif "from" in claim:
            return f"{prefix}.from is allowed only for transition claims"
        check = claim.get("check")
        if not isinstance(check, dict):
            return f"{prefix}.check must be an object"
        if check.get("op") not in OPS:
            return f"{prefix}.check.op is not supported"
        if "left" not in check or "right" not in check:
            return f"{prefix}.check must contain left and right"
        for side in ("left", "right"):
            value = check[side]
            if isinstance(value, (dict, list)):
                return f"{prefix}.check.{side} must be a source expression or literal"
    return None


def write_semantic_claims(path: Path, response: str) -> list[dict[str, Any]]:
    error = validate_semantic_claims(response)
    if error is not None:
        raise ValueError(error)
    claims = parse_semantic_claims(response)
    assert claims is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(claims, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return claims
