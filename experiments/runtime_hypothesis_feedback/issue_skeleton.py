#!/usr/bin/env python3
"""Build a constrained, auditable hypothesis skeleton from a public issue.

The skeleton is a secondary artifact.  It never replaces or edits the issue and
it deliberately leaves source locations, concrete triggers, downstream
consumers, and sanitizer effects unknown.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "issue-skeleton-v2"
UNKNOWN_FIELDS = (
    "concrete_trigger",
    "source_location",
    "downstream_consumer",
    "sanitizer_visible_effect",
)
CLAIM_FIELDS = (
    "operation",
    "affected_value",
    "expected_property",
    "claimed_violation",
    "missing_enforcement",
)


SYSTEM_PROMPT = """You convert a supplied public vulnerability issue into a
small hypothesis skeleton. Use only the supplied issue text. Do not correct,
expand, or reinterpret it using outside knowledge. Return one JSON object and
nothing else.

Required shape:
{
  "claims": {
    "operation": {"value": "...", "evidence_text": "exact issue substring"},
    "affected_value": {"value": "...", "evidence_text": "exact issue substring"},
    "expected_property": {"value": "...", "evidence_text": "exact issue substring"},
    "claimed_violation": {"value": "...", "evidence_text": "exact issue substring"},
    "missing_enforcement": {"value": "...", "evidence_text": "exact issue substring"}
  },
  "root_hypothesis": {
    "predicate": "a conservative, testable restatement of the claimed violation",
    "positive_evidence": ["semantic observations that would support it"],
    "insufficient_evidence": ["observations that do not establish it"]
  }
}

If a claim is absent, use {"value": null, "evidence_text": null}. Every
non-null evidence_text must be a verbatim contiguous substring of the issue.
Do not name or guess any file, function, line, commit, concrete input, caller,
downstream consumer, exploit sink, PoC, or sanitizer behavior. In particular,
the root predicate must test the issue's stated safety property; a generic
success result, non-empty output, or reached function is insufficient unless
the issue itself says otherwise."""


def issue_sha256(issue_text: str) -> str:
    return hashlib.sha256(issue_text.encode("utf-8")).hexdigest()


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("skeleton response must be a JSON object")
    return value


def validate_generated(issue_text: str, generated: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize model output; reject unsupported assertions."""
    claims = generated.get("claims")
    root = generated.get("root_hypothesis")
    if not isinstance(claims, dict) or not isinstance(root, dict):
        raise ValueError("missing claims or root_hypothesis")

    normalized_claims: dict[str, dict[str, str | None]] = {}
    warnings: list[str] = []
    for name in CLAIM_FIELDS:
        claim = claims.get(name)
        if not isinstance(claim, dict):
            raise ValueError(f"missing claim: {name}")
        value = claim.get("value")
        evidence = claim.get("evidence_text")
        if value is None or evidence is None:
            normalized_claims[name] = {"value": None, "evidence_text": None}
            if value is not None or evidence is not None:
                warnings.append(
                    f"{name}: incomplete value/evidence pair was downgraded to unknown"
                )
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} value must be non-empty text")
        if not isinstance(evidence, str) or evidence not in issue_text:
            # A model paraphrase must never become an attributed issue fact.
            # Degrade only that claim to unknown so a sparse issue can still
            # be used; retain an auditable warning in the artifact.
            normalized_claims[name] = {"value": None, "evidence_text": None}
            warnings.append(f"{name}: evidence was not an exact issue substring")
            continue
        normalized_claims[name] = {
            "value": value.strip(),
            "evidence_text": evidence,
        }

    predicate = root.get("predicate")
    positive = root.get("positive_evidence")
    insufficient = root.get("insufficient_evidence")
    if not isinstance(predicate, str) or not predicate.strip():
        raise ValueError("root predicate must be non-empty text")
    for name, value in (("positive_evidence", positive), ("insufficient_evidence", insufficient)):
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError(f"{name} must be a list of non-empty strings")

    return {
        "claims": normalized_claims,
        "root_hypothesis": {
            "predicate": predicate.strip(),
            "positive_evidence": [item.strip() for item in positive],
            "insufficient_evidence": [item.strip() for item in insufficient],
        },
        # These are forcibly unknown, not model-generated fields.
        "unknowns": list(UNKNOWN_FIELDS),
        "validation_warnings": warnings,
    }


def call_deepseek(issue_text: str, api_key: str, model: str = "deepseek-chat") -> dict[str, Any]:
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "PUBLIC ISSUE (verbatim):\n" + issue_text},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 900,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))
    return _extract_json(result["choices"][0]["message"]["content"])


def build_skeleton(
    sample_id: str,
    issue_path: Path,
    output_path: Path,
    api_key: str,
    *,
    force: bool = False,
    generator: Callable[[str, str], dict[str, Any]] = call_deepseek,
) -> dict[str, Any]:
    issue_before = issue_path.read_bytes()
    issue_text = issue_before.decode("utf-8").strip()
    # Hash the exact on-disk bytes, including final newline, so provenance can
    # be checked with ordinary sha256 tools without normalization rules.
    digest = hashlib.sha256(issue_before).hexdigest()

    if output_path.is_file() and not force:
        cached = json.loads(output_path.read_text(encoding="utf-8"))
        if (
            cached.get("schema_version") == SCHEMA_VERSION
            and cached.get("source", {}).get("sha256") == digest
        ):
            return cached

    generated = validate_generated(issue_text, generator(issue_text, api_key))
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "sample_id": sample_id,
        "source": {
            "path": str(issue_path),
            "sha256": digest,
            "immutable": True,
        },
        **generated,
        "action_boundary": {
            "first_candidate_scope": (
                "Exercise the identified operation with a concrete candidate intended "
                "to violate the issue-stated property, then submit it immediately."
            ),
            "not_prerequisites_for_first_submission": [
                "downstream_consumer",
                "sanitizer_visible_effect",
                "complete_vulnerability_path",
            ],
            "after_first_feedback": (
                "Use runtime feedback to decide whether to correct input format, "
                "revise the root trigger, or search for a downstream consumer."
            ),
        },
        "provenance": {
            "kind": "issue_only_secondary_processing",
            "uses_hidden_gt": False,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if issue_path.read_bytes() != issue_before:
        raise RuntimeError("public issue changed while generating its skeleton")
    return artifact


def render_prompt(base_prompt: str, skeleton: dict[str, Any]) -> str:
    public_view = {
        "schema_version": skeleton["schema_version"],
        "claims": skeleton["claims"],
        "root_hypothesis": skeleton["root_hypothesis"],
        "unknowns": skeleton["unknowns"],
        "validation_warnings": skeleton.get("validation_warnings", []),
        "action_boundary": skeleton["action_boundary"],
        "provenance": skeleton["provenance"],
    }
    return (
        base_prompt.rstrip()
        + "\n\n## Issue-derived hypothesis skeleton\n\n"
        + "This is a secondary, issue-only restatement, not ground truth. The public "
          "issue remains authoritative. Unknown fields must be resolved from local "
          "source and runtime evidence; do not treat them as negative claims.\n\n"
        + "```json\n"
        + json.dumps(public_view, indent=2, ensure_ascii=False)
        + "\n```\n"
    )
