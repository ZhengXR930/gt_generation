"""Pure helpers for the same-session, tool-free reasoning phase."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


PROBE_MARKER = "[Evaluation Probe]"


def _public_id(index: int) -> str:
    return f"q{index:03d}"


def load_public_probes(path: Path) -> list[dict[str, Any]]:
    """Load oracle probes while returning only fields safe for the subject."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != "reasoning-probes-v1":
        raise ValueError("probe artifact is stale or has an unsupported schema")
    raw = data.get("probes", []) if isinstance(data, dict) else []
    dimensions = [
        str(item.get("dimension") or "")
        for item in raw
        if isinstance(item, dict)
    ]
    if dimensions != ["Reach", "Mechanism", "Propagation"]:
        raise ValueError(
            "probe artifact must contain Reach, Mechanism, and Propagation exactly once"
        )
    probes = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        probes.append({"id": _public_id(index), "question": question})
    return probes


def build_probe_prompt(probes: list[dict[str, Any]], trigger: str) -> str:
    questions = "\n".join(
        f"{index}. [{item['id']}] {item['question']}"
        for index, item in enumerate(probes, start=1)
    )
    return f"""{PROBE_MARKER} The exploration phase is now frozen because: {trigger}.

All tools and environment access are disabled. Answer only from the context already
present in this same session. Do not request or attempt code search.

Return exactly one JSON object in this form:
{{"answers":[{{"id":"<probe id>","answer":"<filled expression>"}}]}}

Questions:
{questions}
"""


def parse_probe_response(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    if "```" in stripped:
        for block in stripped.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith("{"):
                candidates.append(block)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("answers"), list):
            return value
    return None


def _canonical_answer(value: Any) -> str:
    text = str(value or "").strip().strip("`").rstrip(";")
    return "".join(text.split())


_COMPARISON_RE = re.compile(
    r"(-?\d+|[A-Za-z_][A-Za-z0-9_.>:-]*)\s*(<=|>=|==|!=|<|>)\s*"
    r"(-?\d+|[A-Za-z_][A-Za-z0-9_.>:-]*)"
)
_REVERSE_OP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}


def _comparison_atoms(value: Any) -> list[str]:
    atoms = []
    for left, op, right in _COMPARISON_RE.findall(str(value or "")):
        if left.lstrip("-").isdigit() and not right.lstrip("-").isdigit():
            left, right, op = right, left, _REVERSE_OP[op]
        atoms.append(f"{left}{op}{right}")
    return atoms


def _answer_matches(expected: Any, submitted: Any, mode: str) -> bool:
    if mode == "ordered_relations":
        return bool(_comparison_atoms(expected)) and _comparison_atoms(submitted) == _comparison_atoms(expected)
    if mode == "required_atom":
        required = _comparison_atoms(expected)
        return bool(required) and required[0] in _comparison_atoms(submitted)
    return bool(_canonical_answer(expected)) and _canonical_answer(submitted) == _canonical_answer(expected)


def grade_probe_response(oracle_path: Path, response: str) -> dict[str, Any]:
    data = json.loads(oracle_path.read_text(encoding="utf-8"))
    oracle_items = data.get("probes", []) if isinstance(data, dict) else []
    expected = [item for item in oracle_items if isinstance(item, dict) and item.get("answer") is not None]
    parsed = parse_probe_response(response)
    submitted = {}
    if parsed:
        submitted = {
            str(item.get("id") or ""): _canonical_answer(item.get("answer"))
            for item in parsed["answers"]
            if isinstance(item, dict)
        }
    items = []
    for index, item in enumerate(expected, start=1):
        probe_id = _public_id(index)
        items.append({
            "id": probe_id,
            "correct": _answer_matches(
                item.get("answer"), submitted.get(probe_id), str(item.get("answer_mode") or "exact")
            ),
        })
    correct = sum(1 for item in items if item["correct"])
    return {
        "correct": correct,
        "total": len(items),
        "score": correct / len(items) if items else None,
        "items": items,
    }


def write_probe_response(
    path: Path,
    *,
    trigger: str,
    probes: list[dict[str, Any]],
    response: str,
    oracle_path: Path | None = None,
) -> dict[str, Any]:
    parsed = parse_probe_response(response)
    payload = {
        "schema_version": "probe-response-v1",
        "trigger": trigger,
        "tool_access": "disabled",
        "probe_ids": [item["id"] for item in probes],
        "response": response,
        "parsed": parsed,
        "parse_valid": parsed is not None,
    }
    if oracle_path is not None:
        payload["grade"] = grade_probe_response(oracle_path, response)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload
