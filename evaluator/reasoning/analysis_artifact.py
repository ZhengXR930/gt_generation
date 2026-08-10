"""Validation for the joint fine-trace and semantic-claim artifact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluator.reasoning.fine_trace import validate_fine_trace
from evaluator.reasoning.semantic_claims import validate_semantic_claims


def parse_analysis_artifact(response: str) -> dict[str, Any] | None:
    try:
        value = json.loads((response or "").strip())
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def validate_analysis_artifact(response: str) -> str | None:
    artifact = parse_analysis_artifact(response)
    if artifact is None:
        return "the final answer must be one bare JSON object"
    expected = {"fine_trace", "semantic_claims"}
    if set(artifact) != expected:
        return "the top-level object must contain exactly fine_trace and semantic_claims"

    fine_error = validate_fine_trace(json.dumps(artifact["fine_trace"]))
    if fine_error:
        return f"fine_trace: {fine_error}"
    claim_error = validate_semantic_claims(json.dumps(artifact["semantic_claims"]))
    if claim_error:
        return f"semantic_claims: {claim_error}"
    return None


def write_analysis_artifact(path: Path, response: str) -> dict[str, Any]:
    error = validate_analysis_artifact(response)
    if error is not None:
        raise ValueError(error)
    artifact = parse_analysis_artifact(response)
    assert artifact is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return artifact
