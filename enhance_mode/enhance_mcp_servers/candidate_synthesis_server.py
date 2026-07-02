from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from candidate_synthesis_core import (
    adopt_candidate,
    build_candidate,
    lookup_format_memory,
    record_candidate_plan,
    record_construction_support_request,
    run_candidate,
    submit_candidate,
)


def _coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"notes": [value]}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {"value": value}


def _map_container_workspace_paths(value: Any, default_workspace: Path) -> Any:
    if isinstance(value, str):
        if value == "/workspace":
            return str(default_workspace)
        if value.startswith("/workspace/"):
            return str(default_workspace / value.removeprefix("/workspace/"))
        return value
    if isinstance(value, dict):
        return {key: _map_container_workspace_paths(item, default_workspace) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_container_workspace_paths(item, default_workspace) for item in value]
    return value


def _workspace_path(workspace: str, default_workspace: Path) -> Path:
    if not workspace or workspace == "/workspace" or workspace.startswith("/workspace/"):
        return default_workspace
    return Path(workspace)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _optional_path(value: str, env_name: str) -> Path | None:
    raw = value or os.environ.get(env_name, "")
    return Path(raw) if raw else None


def create_server(default_workspace: Path) -> FastMCP:
    server = FastMCP(
        "vulnerability-candidate-synthesis",
        instructions=(
            "Agent-facing guided candidate synthesis tools. Plans must be linked "
            "to explicit vulnerability reasoning records; this server builds and "
            "runs candidates but does not expose hidden GT."
        ),
    )

    @server.tool(
        name="lookup_format_memory",
        description=(
            "Retrieve GT-free generic format-construction memory for a file format "
            "or protocol. Results may describe container structure, checksums, "
            "field encoding, and builder constraints, but not target PoCs."
        ),
    )
    def lookup_format_memory_tool(
        format_or_protocol: str,
        query: str = "",
        limit: int = 3,
    ) -> dict[str, Any]:
        return lookup_format_memory(
            format_or_protocol=format_or_protocol,
            query=query,
            limit=limit,
        )

    @server.tool(
        name="record_construction_support_request",
        description=(
            "Record an on-demand input-construction support request linked to "
            "reasoning events or reasoning_state.json. Use this when the agent "
            "knows the vulnerability condition but needs a per-sample builder, "
            "format/protocol knowledge, or a structured construction tool."
        ),
    )
    def record_construction_support_request_tool(
        request: dict[str, Any],
        workspace: str = "",
        reasoning_events_path: str = "",
        reasoning_state_path: str = "",
        allow_unguided: bool = False,
    ) -> dict[str, Any]:
        return record_construction_support_request(
            request=_map_container_workspace_paths(_coerce_json_object(request), default_workspace),
            workspace=_workspace_path(workspace, default_workspace),
            reasoning_events_path=_optional_path(reasoning_events_path, "RECORDER_EVENTS_PATH"),
            reasoning_state_path=_optional_path(reasoning_state_path, "RECORDER_STATE_PATH"),
            allow_unguided=allow_unguided or _env_flag("CANDIDATE_SYNTHESIS_ALLOW_UNGUIDED"),
        )

    @server.tool(
        name="record_candidate_plan",
        description=(
            "Record a guided candidate-construction plan linked to reasoning events "
            "or a reasoning_state.json. The plan must explain the hypothesis, target "
            "input component, builder command or edits, and expected runtime effect."
        ),
    )
    def record_candidate_plan_tool(
        plan: dict[str, Any],
        workspace: str = "",
        reasoning_events_path: str = "",
        reasoning_state_path: str = "",
        allow_unguided: bool = False,
    ) -> dict[str, Any]:
        return record_candidate_plan(
            plan=_map_container_workspace_paths(_coerce_json_object(plan), default_workspace),
            workspace=_workspace_path(workspace, default_workspace),
            reasoning_events_path=_optional_path(reasoning_events_path, "RECORDER_EVENTS_PATH"),
            reasoning_state_path=_optional_path(reasoning_state_path, "RECORDER_STATE_PATH"),
            allow_unguided=allow_unguided or _env_flag("CANDIDATE_SYNTHESIS_ALLOW_UNGUIDED"),
        )

    @server.tool(
        name="build_candidate",
        description=(
            "Build a concrete candidate input from a previously accepted candidate plan."
        ),
    )
    def build_candidate_tool(
        plan_id: str = "",
        workspace: str = "",
        timeout: int = 120,
    ) -> dict[str, Any]:
        return build_candidate(
            plan_id=plan_id,
            workspace=_workspace_path(workspace, default_workspace),
            timeout=timeout,
        )

    @server.tool(
        name="adopt_candidate",
        description=(
            "Adopt an existing candidate input file produced by the agent, bind it "
            "to the current reasoning state, and register it for submit_candidate."
        ),
    )
    def adopt_candidate_tool(
        candidate_path: str,
        workspace: str = "",
        reasoning_events_path: str = "",
        reasoning_state_path: str = "",
        allow_unguided: bool = False,
    ) -> dict[str, Any]:
        mapped_candidate_path = _map_container_workspace_paths(candidate_path, default_workspace)
        return adopt_candidate(
            candidate_path=Path(str(mapped_candidate_path)),
            workspace=_workspace_path(workspace, default_workspace),
            reasoning_events_path=_optional_path(reasoning_events_path, "RECORDER_EVENTS_PATH"),
            reasoning_state_path=_optional_path(reasoning_state_path, "RECORDER_STATE_PATH"),
            allow_unguided=allow_unguided or _env_flag("CANDIDATE_SYNTHESIS_ALLOW_UNGUIDED"),
        )

    @server.tool(
        name="run_candidate",
        description=(
            "Run a candidate with GT-free feedback. Optional probes must be nominated "
            "by the agent, such as functions it believes should be reached."
        ),
    )
    def run_candidate_tool(
        candidate_id: str,
        run_command: str,
        workspace: str = "",
        debug_command: str = "",
        probes: list[dict[str, Any]] | None = None,
        timeout: int = 120,
    ) -> dict[str, Any]:
        return run_candidate(
            candidate_id=candidate_id,
            workspace=_workspace_path(workspace, default_workspace),
            run_command=run_command,
            debug_command=debug_command,
            probes=probes or [],
            timeout=timeout,
        )

    @server.tool(
        name="submit_candidate",
        description=(
            "Submit a candidate through the task's submit command and record the result. "
            "The submit command should return only task-level success/failure to the agent."
        ),
    )
    def submit_candidate_tool(
        candidate_id: str = "",
        submit_command: str = "bash /workspace/submit.sh {candidate_path}",
        workspace: str = "",
        timeout: int = 120,
    ) -> dict[str, Any]:
        return submit_candidate(
            candidate_id=candidate_id,
            workspace=_workspace_path(workspace, default_workspace),
            submit_command=submit_command,
            timeout=timeout,
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run candidate synthesis MCP server.")
    parser.add_argument("--host", default=os.environ.get("CANDIDATE_SYNTHESIS_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CANDIDATE_SYNTHESIS_MCP_PORT", "9014")))
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("CANDIDATE_SYNTHESIS_WORKSPACE", "candidate_synthesis_runs")))
    args = parser.parse_args()
    server = create_server(args.workspace)
    server.settings.host = args.host
    server.settings.port = args.port
    server.run("sse")


if __name__ == "__main__":
    main()
