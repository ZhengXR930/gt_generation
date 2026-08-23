"""Shared PoC task and analysis.json contract for all harnesses.

The canonical task prompt is the OpenHands evaluation prompt under
``poc_generation/poc_generator/openhands_backend/template/prompt.txt`` because OpenHands has the
largest evaluation coverage. A legacy top-level template path is accepted only as a fallback.  Non-OpenHands harnesses reuse that prompt and only
rewrite the workspace path when they run outside an OpenHands /workspace mount.
"""

from __future__ import annotations

import json
from pathlib import Path


PROMPT_CONTRACT_ID = "openhands-poc-generation-prompt-v1"
ANALYSIS_CONTRACT_ID = "analysis-json-sample-fine_trace-vuln_logic-v1"
ANALYSIS_REQUIRED_KEYS = ("sample_id", "fine_trace", "vuln_logic")
TRACE_REQUIRED_KEYS = ("step", "file", "function", "line", "var", "code", "role", "note")
VALID_TRACE_ROLES = {"source", "root_cause", "sink", "intermediate", "null"}
VALID_RELATION_OPS = {"eq", "ne", "lt", "le", "gt", "ge", "same_object"}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def canonical_openhands_prompt_path() -> Path:
    candidates = (
        repo_root() / "poc_generation" / "poc_generator" / "openhands_backend" / "template" / "prompt.txt",
        repo_root() / "poc_generation" / "poc_generator" / "template" / "prompt.txt",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def canonical_openhands_prompt_text() -> str:
    path = canonical_openhands_prompt_path()
    if not path.is_file():
        raise FileNotFoundError(f"missing canonical OpenHands prompt: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def render_poc_task_prompt(
    *,
    sample_id: str,
    workspace: str,
    max_iter: int | str,
    skill_packet_enabled: bool = False,
) -> str:
    """Render the OpenHands canonical PoC prompt for any harness.

    ``max_iter`` and ``skill_packet_enabled`` are accepted so all adapters call a
    uniform interface.  The OpenHands prompt already describes the configured
    iteration budget generically, and skill packet exposure is handled by each
    adapter through README/native skill mechanisms, not by changing the task
    prompt.
    """
    del max_iter, skill_packet_enabled
    text = canonical_openhands_prompt_text()
    text = text.replace("/workspace", workspace)
    text = text.replace("<current sample id>", sample_id)
    return text


def validate_analysis_json_text(text: str, *, expected_sample_id: str | None = None) -> tuple[bool, list[str]]:
    """Deterministic structural validation shared by harness adapters.

    This intentionally checks schema shape only. Reasoning quality is evaluated
    by the diagnostic evaluator, not by this helper.
    """
    errors: list[str] = []
    try:
        value = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return False, [f"invalid JSON: {type(exc).__name__}: {exc}"]
    if not isinstance(value, dict):
        return False, ["analysis must be one JSON object"]
    keys = set(value)
    required = set(ANALYSIS_REQUIRED_KEYS)
    if keys != required:
        errors.append(f"top-level keys must be exactly {sorted(required)}; got {sorted(keys)}")
    sample_id = value.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        errors.append("sample_id must be a non-empty string")
    elif expected_sample_id is not None and sample_id != expected_sample_id:
        errors.append(f"sample_id must equal {expected_sample_id!r}; got {sample_id!r}")
    trace = value.get("fine_trace")
    if not isinstance(trace, list) or not trace:
        errors.append("fine_trace must be a non-empty list")
    else:
        for index, step in enumerate(trace, 1):
            if not isinstance(step, dict):
                errors.append(f"fine_trace[{index}] must be an object")
                continue
            missing = [key for key in TRACE_REQUIRED_KEYS if key not in step]
            if missing:
                errors.append(f"fine_trace[{index}] missing keys {missing}")
            if step.get("step") != index:
                errors.append(f"fine_trace[{index}] step must be {index}")
            if "line" in step and not isinstance(step.get("line"), int):
                errors.append(f"fine_trace[{index}].line must be an integer")
            role = step.get("role")
            if role not in VALID_TRACE_ROLES:
                errors.append(f"fine_trace[{index}].role must be one of {sorted(VALID_TRACE_ROLES)}")
    vuln_logic = value.get("vuln_logic")
    if not isinstance(vuln_logic, dict):
        errors.append("vuln_logic must be an object")
    else:
        for name in ("source", "root_cause", "sink"):
            if name in vuln_logic and not isinstance(vuln_logic[name], dict):
                errors.append(f"vuln_logic.{name} must be an object when present")
        for name in ("root_cause", "sink"):
            relation = (vuln_logic.get(name) or {}).get("relation") if isinstance(vuln_logic.get(name), dict) else None
            if relation is not None:
                if not isinstance(relation, dict) or set(relation) != {"op", "left", "right"}:
                    errors.append(f"vuln_logic.{name}.relation must have exactly op, left, right")
                elif relation.get("op") not in VALID_RELATION_OPS:
                    errors.append(f"vuln_logic.{name}.relation.op invalid: {relation.get('op')!r}")
        propagation = vuln_logic.get("propagation")
        if propagation is not None and not isinstance(propagation, list):
            errors.append("vuln_logic.propagation must be a list when present")
    return not errors, errors
