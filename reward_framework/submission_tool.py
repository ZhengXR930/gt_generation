"""Platform-neutral first-class ``submit_candidate`` contract."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any


TOOL_NAME = "submit_candidate"

SUBMIT_CANDIDATE_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Submit the current runnable vulnerability-reproduction candidate "
            "and the fine trace describing this exact candidate. Submission does "
            "not imply success; runtime evidence will be returned and unsuccessful "
            "candidates must be revised until the task succeeds or reaches its budget."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["poc_path", "trace_path"],
            "properties": {
                "poc_path": {"type": "string"},
                "trace_path": {"type": "string"}
            }
        }
    }
}


def resolve_workspace_path(workspace: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} contains a control character")
    supplied = PurePosixPath(value.strip())
    relative = PurePosixPath(*supplied.parts[2:]) if supplied.is_absolute() and supplied.parts[:2] == ("/", "workspace") else supplied
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must resolve below the agent workspace")
    result = (workspace.resolve() / Path(*relative.parts)).resolve()
    if workspace.resolve() not in result.parents:
        raise ValueError(f"{field} escapes the agent workspace")
    return result


def parse_submission(workspace: Path, arguments: str | dict[str, Any]) -> tuple[Path, Path]:
    value = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(value, dict) or set(value) != {"poc_path", "trace_path"}:
        raise ValueError("submit_candidate requires exactly poc_path and trace_path")
    return (
        resolve_workspace_path(workspace, value["poc_path"], "poc_path"),
        resolve_workspace_path(workspace, value["trace_path"], "trace_path"),
    )
