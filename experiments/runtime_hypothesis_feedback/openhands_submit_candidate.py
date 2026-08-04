#!/usr/bin/env python3
"""OpenHands adapter for the platform-neutral ``submit_candidate`` tool."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path

import openhands.agenthub.codeact_agent.function_calling as function_calling
import openhands.core.main as oh_main
from openhands.core.config import parse_arguments, setup_config_from_args
from openhands.core.setup import generate_sid
from openhands.events import EventSource
from openhands.events.action import (
    AgentFinishAction,
    CmdRunAction,
    MessageAction,
    NullAction,
)
from openhands.io import read_task

from experiments.runtime_hypothesis_feedback.submit_candidate_tool import (
    SUBMIT_CANDIDATE_TOOL,
    TOOL_NAME,
    parse_submission_arguments,
    submission_response_triggered,
    submission_command,
)
from experiments.runtime_hypothesis_feedback.state_monitor import is_submission_action


_ORIGINAL_RESPONSE_TO_ACTIONS = function_calling.response_to_actions
SUBMISSION_ACTION_TIMEOUT_SECONDS = 300


def install_closed_network_runtime_route() -> None:
    """Reach the action server directly when Docker suppresses published ports.

    Docker intentionally does not expose host port mappings for an ``internal``
    network.  The host can still reach the container's bridge address, while the
    container has no default route or DNS access to the public Internet.
    """
    network = os.getenv("OPENHANDS_RUNTIME_DOCKER_NETWORK", "").strip()
    if not network:
        return
    from openhands.runtime.impl.docker.docker_runtime import DockerRuntime

    if getattr(DockerRuntime, "_closed_network_route_installed", False):
        return
    original_init_container = DockerRuntime._init_container

    def closed_network_init_container(runtime):
        original_init_container(runtime)
        runtime.container.reload()
        networks = runtime.container.attrs["NetworkSettings"]["Networks"]
        endpoint = networks.get(network) or {}
        address = endpoint.get("IPAddress")
        if not address:
            raise RuntimeError(
                f"runtime container did not join closed Docker network {network!r}"
            )
        runtime.api_url = f"http://{address}:{runtime._container_port}"

    DockerRuntime._init_container = closed_network_init_container
    DockerRuntime._closed_network_route_installed = True


def invalid_submission_command(error: Exception) -> str:
    """Return a model-visible tool failure instead of terminating the session."""
    message = (
        "submit_candidate rejected these arguments: "
        f"{error}. Correct the paths or JSON and call submit_candidate again."
    )
    # Commands run in a persistent OpenHands shell. Keep the non-zero tool
    # result inside a subshell so a rejected call does not kill that shell.
    return f"printf '%s\\n' {shlex.quote(message)} >&2; (exit 2)"


def _history_has_triggered_submission(history) -> bool:
    """Use only native tool observations, never model claims, as success proof."""
    for event in history:
        metadata = getattr(event, "tool_call_metadata", None)
        if not metadata or getattr(metadata, "function_name", None) != TOOL_NAME:
            continue
        if submission_response_triggered(getattr(event, "content", None)):
            return True
    return False


def _log_tool_event(kind: str, **payload) -> None:
    raw_path = os.getenv("SUBMIT_CANDIDATE_TOOL_LOG", "")
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def response_to_actions_with_submit_candidate(response):
    """Lower a native submit tool call to the runtime's existing bash action."""
    translated = copy.deepcopy(response)
    submit_call_indexes: list[int] = []
    tool_calls = getattr(translated.choices[0].message, "tool_calls", None) or []
    for index, tool_call in enumerate(tool_calls):
        if tool_call.function.name != TOOL_NAME:
            continue
        try:
            command = submission_command(tool_call.function.arguments)
            poc_path, trace_path = parse_submission_arguments(
                tool_call.function.arguments
            )
            _log_tool_event(
                "tool_selected",
                poc_path=poc_path,
                trace_path=trace_path,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            command = invalid_submission_command(exc)
            _log_tool_event("tool_arguments_rejected", error=str(exc))
        tool_call.function.name = "execute_bash"
        tool_call.function.arguments = json.dumps(
            {"command": command, "is_input": "false"}
        )
        submit_call_indexes.append(index)

    actions = _ORIGINAL_RESPONSE_TO_ACTIONS(translated)
    for index in submit_call_indexes:
        tool_call = tool_calls[index]
        tool_call.function.name = TOOL_NAME
        # Preserve the portable arguments in the model-facing trajectory while
        # the returned CmdRunAction retains the safely lowered bash command.
        original_call = response.choices[0].message.tool_calls[index]
        tool_call.function.arguments = original_call.function.arguments
        actions[index].tool_call_metadata.function_name = TOOL_NAME
        # Runtime verification includes sanitizer execution, GDB probes, and a
        # bounded Reward Agent diagnosis. The ordinary interactive-shell soft
        # timeout is too short and leaves submit.sh running in the persistent
        # PTY, so later native submissions collide with that process. A submit
        # action is a synchronous protocol boundary and must return its complete
        # structured result to the coding agent.
        actions[index].set_hard_timeout(
            SUBMISSION_ACTION_TIMEOUT_SECONDS,
            blocking=True,
        )
    return actions


def install_submit_candidate_tool(agent, event_stream=None) -> None:
    """Expose the tool once and install its OpenHands action lowering."""
    if not any(tool["function"]["name"] == TOOL_NAME for tool in agent.tools):
        agent.tools.append(copy.deepcopy(SUBMIT_CANDIDATE_TOOL))
    function_calling.response_to_actions = response_to_actions_with_submit_candidate
    if getattr(agent, "_submit_candidate_tool_enforced", False):
        return
    original_step = agent.step

    def first_class_submission_step(state):
        terminal_guard = os.getenv("SUBMIT_CANDIDATE_TERMINAL_GUARD", "0") == "1"
        finalizing_trace = bool(state.extra_data.get("fine_trace_finalization"))
        if (
            terminal_guard
            and not finalizing_trace
            and _history_has_triggered_submission(state.history)
        ):
            _log_tool_event("terminal_success")
            return AgentFinishAction(
                final_thought=(
                    "A valid submit_candidate observation proves that the target "
                    "vulnerability was triggered."
                ),
                task_completed="true",
            )
        action = original_step(state)
        if (
            terminal_guard
            and isinstance(action, AgentFinishAction)
            and not finalizing_trace
        ):
            _log_tool_event("premature_finish_blocked")
            if event_stream is None:
                raise RuntimeError(
                    "terminal guard requires the OpenHands event stream"
                )
            event_stream.add_event(
                MessageAction(
                    content=(
                        "[Terminal guard] The task has not reached a valid endpoint: "
                        "no successful vulnerability-triggering submit_candidate "
                        "result has been observed and iteration budget remains. "
                        "Continue autonomously, construct a revised runnable "
                        "candidate, and submit it. Do not wait for user input."
                    ),
                    wait_for_response=False,
                ),
                EventSource.USER,
            )
            return NullAction()
        if not isinstance(action, CmdRunAction) or not is_submission_action(
            {
                "source": "agent",
                "action": "run",
                "args": {"command": action.command},
            }
        ):
            return action
        metadata = getattr(action, "tool_call_metadata", None)
        if metadata and metadata.function_name == TOOL_NAME:
            return action
        _log_tool_event("direct_submit_blocked")
        action.command = (
            "printf '%s\\n' 'Direct submit.sh invocation is disabled in this "
            "experiment. Write the PoC and trace as separate actions, then call "
            "the submit_candidate tool with their /workspace paths.' >&2; (exit 2)"
        )
        return action

    agent.step = first_class_submission_step
    agent._submit_candidate_tool_enforced = True


def main() -> None:
    install_closed_network_runtime_route()
    args = parse_arguments()
    config = setup_config_from_args(args)
    task_str = read_task(args, config.cli_multiline_input)
    if not task_str:
        raise ValueError("No task provided")
    initial_user_action = MessageAction(content=task_str)
    sid = generate_sid(config, args.name)
    original_create_controller = oh_main.create_controller

    def tool_create_controller(agent, runtime, app_config, replay_events=None):
        controller, initial_state = original_create_controller(
            agent, runtime, app_config, replay_events=replay_events
        )
        install_submit_candidate_tool(agent, runtime.event_stream)
        return controller, initial_state

    oh_main.create_controller = tool_create_controller
    asyncio.run(
        oh_main.run_controller(
            config=config,
            initial_user_action=initial_user_action,
            sid=sid,
            fake_user_response_fn=(
                None if args.no_auto_continue else oh_main.auto_continue_response
            ),
        )
    )


if __name__ == "__main__":
    main()
