"""OpenHands entrypoint that adds the evaluation fine-trace final turn.

The upstream OpenHands checkout stays pinned and unmodified.  This module
installs the small evaluation-specific lifecycle overlay before delegating to
``openhands.core.main``:

* persist the last trace submitted with a PoC when one exists;
* at the iteration limit, checkpoint before asking for the final trace;
* run the final trace turn without tools; and
* validate and persist the resulting bare JSON array.
"""

from __future__ import annotations

import json
import inspect
import os
import re
import runpy
import shutil
import tempfile
from pathlib import Path
from typing import Any


FINE_TRACE_FINAL_MARKER = "[Fine Trace Finalization]"
_MAX_FORMAT_RETRIES = 2
_SUBMIT_COMMAND_TIMEOUT_SECONDS = 120
_REWARD_SUBMIT_COMMAND = re.compile(
    r"(?:^|(?:&&|\|\||;|\n)\s*)"
    r"(?:python3\s+)?/workspace/\.reward_framework/submit_candidate\.py"
    r"(?:\s|$)"
)


def _capture_enabled() -> bool:
    return (
        os.environ.get("OPENHANDS_HARNESS_MODE", "evaluation") == "evaluation"
        and os.environ.get("OPENHANDS_CAPTURE_FINE_TRACE", "0") == "1"
    )


def _semantic_claim_mode() -> bool:
    return os.environ.get("OPENHANDS_FINAL_ARTIFACT", "fine_trace") == "semantic_claims"


def _finalization_key() -> str:
    return "semantic_claim_finalization" if _semantic_claim_mode() else "fine_trace_finalization"


def _force_started_key() -> str:
    return "semantic_claim_force_started" if _semantic_claim_mode() else "fine_trace_force_started"


def _final_deliverable_instruction() -> str:
    if _semantic_claim_mode():
        return (
            "Return ONLY one bare JSON array of semantic claim objects. Each object "
            "has kind, at, and check. kind is required, observed, or transition; "
            "a transition also has from. at/from each contain file, function, and "
            "integer line. check contains op, left, and right; op is eq, ne, lt, "
            "le, gt, ge, or same_object, and operands are source expressions or "
            "literals. A required claim states a safety obligation, an observed "
            "claim states an unsafe state in the vulnerable execution, and a "
            "transition compares the left operand at from with the right operand "
            "at the later at event. Use only the evidence already recovered in "
            "this checkpoint. Do not emit prose, Markdown, GT identifiers, or "
            "fine-trace fields."
        )
    return (
        "Return the final deliverable specified in the initial task prompt now. "
        "Output ONLY the GT-shaped JSON fine-trace array with the required fields "
        '"step", "file", "function", "line", "var", "code", and "note". '
        'Do not output a "depends_on" field.'
    )


def _validate_final_response(response: str) -> str | None:
    if _semantic_claim_mode():
        from evaluator.reasoning.semantic_claims import validate_semantic_claims

        return validate_semantic_claims(response)
    from evaluator.reasoning.fine_trace import validate_fine_trace

    return validate_fine_trace(response)


def _forced_finalization_trigger() -> str | None:
    value = os.environ.get("OPENHANDS_FORCE_FINE_TRACE_FINALIZATION", "").strip()
    return value or None


def _finalization(controller: Any) -> dict[str, Any] | None:
    value = controller.state.extra_data.get(_finalization_key())
    return value if isinstance(value, dict) else None


def _is_finalizing(controller: Any) -> bool:
    value = _finalization(controller)
    return bool(value and value.get("status") == "answering")


def _is_submit_command(action: Any) -> bool:
    command = getattr(action, "command", "")
    return (
        isinstance(command, str)
        and not bool(getattr(action, "is_input", False))
        and (
            "submit.sh" in command
            or bool(_REWARD_SUBMIT_COMMAND.search(command.strip()))
        )
    )


def _make_submit_command_blocking(action: Any) -> None:
    """Wait for synchronous PoC evaluation instead of exposing a soft timeout."""
    if _is_submit_command(action):
        action.set_hard_timeout(_SUBMIT_COMMAND_TIMEOUT_SECONDS, blocking=True)


def _get_agent_messages(agent: Any, events: Any, state: Any) -> Any:
    """Call the pristine or an evolved OpenHands message-builder contract."""
    method = agent._get_messages
    signature = inspect.signature(method)
    try:
        signature.bind(events, state)
    except TypeError:
        # Validate the pristine call shape before executing it. This avoids
        # mistaking a TypeError raised *inside* an evolved implementation for
        # an old method signature.
        signature.bind(events)
        return method(events)
    return method(events, state)


def _submitted_trace() -> str | None:
    workspace = os.environ.get("OPENHANDS_TASK_WORKSPACE", "").strip()
    default_marker = str(Path(workspace) / ".poc_submission_recorded") if workspace else ""
    default_trace = str(Path(workspace) / ".latest_candidate_trace.json") if workspace else ""
    marker = os.environ.get(
        "OPENHANDS_POC_SUBMISSION_MARKER", default_marker
    ).strip()
    trace = os.environ.get(
        "OPENHANDS_LATEST_SUBMISSION_TRACE", default_trace
    ).strip()
    if not marker or not trace or not Path(marker).is_file() or not Path(trace).is_file():
        return None
    try:
        response = Path(trace).read_text(encoding="utf-8")
        from evaluator.reasoning.fine_trace import validate_fine_trace

        return response if validate_fine_trace(response) is None else None
    except (OSError, UnicodeError):
        return None


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        destination.mkdir()


def _write_pre_finalization_checkpoint(controller: Any) -> Path | None:
    raw = os.environ.get("OPENHANDS_PRE_FINALIZATION_CHECKPOINT", "").strip()
    if not raw:
        return None
    destination = Path(raw).expanduser().resolve()
    if destination.exists():
        return destination

    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=destination.name + ".", dir=str(parent)))
    try:
        controller.state.save_to_session(
            controller.event_stream.sid,
            controller.event_stream.file_store,
            controller.event_stream.user_id,
        )
        file_store_root = getattr(controller.event_stream.file_store, "root", "")
        if not file_store_root:
            raise RuntimeError("pre-finalization checkpoint requires a local file store")
        _copy_tree(Path(file_store_root), staging / "file")
        cache_root = os.environ.get("OPENHANDS_CACHE_DIR", "").strip()
        if cache_root:
            _copy_tree(Path(cache_root), staging / "cache")
        else:
            (staging / "cache").mkdir()
        from openhands.events.serialization.event import event_to_trajectory

        trajectory = [
            event_to_trajectory(event, include_screenshots=False)
            for event in controller.state.history
        ]
        (staging / "trajectory").write_text(
            json.dumps(trajectory, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (staging / "metadata.json").write_text(
            json.dumps(
                {
                    "phase": "pre_fine_trace_finalization",
                    "iteration": controller.state.iteration,
                    "max_iterations": controller.state.max_iterations,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        staging.replace(destination)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _start_finalization(controller: Any, trigger: str) -> bool:
    if not _capture_enabled():
        return False
    current = _finalization(controller)
    if current and current.get("status") in {"answering", "completed"}:
        return False

    checkpoint = _write_pre_finalization_checkpoint(controller)
    controller._pending_action = None
    if hasattr(controller.agent, "pending_actions"):
        controller.agent.pending_actions.clear()
    controller.state.extra_data[_finalization_key()] = {
        "status": "answering",
        "trigger": trigger,
        "tool_access": "disabled",
        "started_iteration": controller.state.iteration,
        "pre_finalization_checkpoint": str(checkpoint) if checkpoint else None,
    }

    from openhands.events import EventSource
    from openhands.events.action import MessageAction

    controller.event_stream.add_event(
        MessageAction(
            content=(
                f"{FINE_TRACE_FINAL_MARKER} The PoC task has ended because: "
                f"{trigger}. {_final_deliverable_instruction()}"
            ),
            wait_for_response=False,
        ),
        EventSource.USER,
    )
    return True


async def _persist_trace(controller: Any, response: str, trigger: str) -> None:
    from openhands.core.schema import AgentState

    if _semantic_claim_mode():
        from evaluator.reasoning.semantic_claims import write_semantic_claims

        output = Path(
            os.environ.get("OPENHANDS_SEMANTIC_CLAIMS_OUTPUT")
            or Path(os.environ.get("LOG_DIR") or ".") / "semantic_claims.json"
        )
        writer = write_semantic_claims
    else:
        from evaluator.reasoning.fine_trace import write_fine_trace

        output = Path(
            os.environ.get("OPENHANDS_FINE_TRACE_OUTPUT")
            or Path(os.environ.get("LOG_DIR") or ".") / "fine_trace.json"
        )
        writer = write_fine_trace
    try:
        writer(output, response)
        result = {
            "status": "completed",
            "trigger": trigger,
            "output": str(output),
            "completed_iteration": controller.state.iteration,
            "pre_finalization_checkpoint": (
                (_finalization(controller) or {}).get("pre_finalization_checkpoint")
            ),
        }
    except Exception as exc:
        result = {
            "status": "failed",
            "trigger": trigger,
            "error": f"{type(exc).__name__}: {exc}",
        }
    controller.state.extra_data[_finalization_key()] = result
    controller.state.outputs = {
        **controller.state.outputs,
        _finalization_key(): result,
    }
    controller.state.metrics.merge(controller.state.local_metrics)
    await controller.set_agent_state_to(AgentState.FINISHED)


async def _complete_trace(controller: Any, response: str) -> None:
    finalization = _finalization(controller)
    if not finalization or finalization.get("status") != "answering":
        return
    from openhands.events import EventSource
    from openhands.events.action import MessageAction

    attempts = int(finalization.get("attempts") or 0) + 1
    finalization["attempts"] = attempts
    error = _validate_final_response(response)
    if error is not None and attempts <= _MAX_FORMAT_RETRIES:
        controller.event_stream.add_event(
            MessageAction(
                content=(
                    f"{FINE_TRACE_FINAL_MARKER} The final deliverable was not "
                    f"accepted: {error}. {_final_deliverable_instruction()}"
                ),
                wait_for_response=False,
            ),
            EventSource.USER,
        )
        return
    if error is not None:
        from openhands.core.schema import AgentState

        finalization["status"] = "failed"
        finalization["error"] = error
        controller.state.outputs = {
            **controller.state.outputs,
            _finalization_key(): finalization,
        }
        await controller.set_agent_state_to(AgentState.FINISHED)
        return
    await _persist_trace(
        controller, response, str(finalization.get("trigger") or "")
    )


def install_fine_trace_overlay() -> None:
    from openhands.agenthub.codeact_agent.codeact_agent import CodeActAgent
    from openhands.controller.agent_controller import AgentController
    from openhands.events.action import AgentFinishAction
    from openhands.memory.condenser.condenser import Condensation, View

    if getattr(AgentController, "_fine_trace_overlay_installed", False):
        return

    original_handle_action = AgentController._handle_action
    original_step = AgentController._step
    original_agent_step = CodeActAgent.step
    original_set_agent_state_to = AgentController.set_agent_state_to
    original_is_stuck = AgentController._is_stuck

    async def handle_action(controller, action):
        _make_submit_command_blocking(action)
        if not isinstance(action, AgentFinishAction) or not _capture_enabled():
            return await original_handle_action(controller, action)
        submitted = _submitted_trace()
        if submitted is not None:
            await _persist_trace(controller, submitted, "last_poc_submission")
            return
        response = action.final_thought or action.thought or json.dumps(
            action.outputs, ensure_ascii=False
        )
        if _is_finalizing(controller):
            await _complete_trace(controller, response)
            return
        # A normal finish ends tool use. Always freeze that state and ask for the
        # deliverable in a separate no-tools turn, even if the first finish text
        # already happens to be valid trace JSON.
        _start_finalization(controller, "agent_finished")

    async def controller_step(controller):
        forced_trigger = _forced_finalization_trigger()
        if (
            _capture_enabled()
            and forced_trigger
            and not _is_finalizing(controller)
            and not controller.state.extra_data.get(_force_started_key())
        ):
            controller.state.extra_data[_force_started_key()] = True
            _start_finalization(controller, forced_trigger)
            return
        if (
            _capture_enabled()
            and not _is_finalizing(controller)
            and not controller.state.extra_data.get(_force_started_key())
            and controller.state.iteration >= controller.state.max_iterations
        ):
            submitted = _submitted_trace()
            if submitted is not None:
                _write_pre_finalization_checkpoint(controller)
                await _persist_trace(controller, submitted, "last_poc_submission")
            else:
                _start_finalization(controller, "iteration_limit")
            return
        if not _is_finalizing(controller):
            return await original_step(controller)

        maximum = controller.state.max_iterations
        controller.state.max_iterations = controller.state.iteration + 1
        try:
            return await original_step(controller)
        finally:
            controller.state.max_iterations = maximum

    async def set_agent_state_to(controller, new_state):
        # Stuck-loop and controller errors used to terminate an otherwise useful
        # checkpoint without giving the model its required no-tools final turn.
        # Freeze the evidence gathered so far and finalize it instead.  Errors
        # raised by the finalization turn itself still terminate normally.
        from openhands.core.schema import AgentState

        if (
            new_state == AgentState.ERROR
            and _capture_enabled()
            and not _is_finalizing(controller)
        ):
            trigger = controller.state.last_error or "agent_error"
            _start_finalization(controller, f"agent_error: {trigger}")
            return
        return await original_set_agent_state_to(controller, new_state)

    def is_stuck(controller):
        # The frozen exploration history may itself contain the repeated action
        # that ended the episode.  It is evidence for finalization, not a reason
        # to prevent the one no-tools response.
        if _is_finalizing(controller):
            return False
        return original_is_stuck(controller)

    def agent_step(agent, state):
        finalization = state.extra_data.get(_finalization_key())
        if not (
            isinstance(finalization, dict)
            and finalization.get("status") == "answering"
        ):
            return original_agent_step(agent, state)

        agent.pending_actions.clear()
        condensed = agent.condenser.condensed_history(state)
        if isinstance(condensed, Condensation):
            return condensed.action
        assert isinstance(condensed, View)
        # Adaptive harness revisions may extend the private message builder
        # with controller state (for example, to preserve a pending submission
        # obligation across condensation).  Keep the capture/finalization shim
        # compatible with both pristine OpenHands and such revisions.
        messages = _get_agent_messages(agent, condensed.events, state)
        # The normal CodeAct system prompt strongly teaches tool syntax.  Merely
        # omitting the tools parameter is not enough for DeepSeek: it often emits
        # a textual execute_bash request instead of the required JSON.  Replace
        # that system instruction for this isolated, no-tools final turn while
        # preserving the checkpoint conversation/evidence that follows it.
        from openhands.core.message import Message, TextContent

        finalizer_system = Message(
            role="system",
            content=[
                TextContent(
                    text=(
                        "You are an evaluation artifact finalizer. Tool use is "
                        "disabled and must never be requested or described. "
                        + _final_deliverable_instruction()
                        + " Do not emit XML, DSML, or tool_calls."
                    )
                )
            ],
            force_string_serializer=True,
        )
        if messages and messages[0].role == "system":
            messages[0] = finalizer_system
        else:
            messages.insert(0, finalizer_system)
        response = agent.llm.completion(
            messages=agent.llm.format_messages_for_llm(messages),
            extra_body={"metadata": state.to_llm_metadata(agent_name=agent.name)},
        )
        from evaluator.reasoning.fine_trace import unwrap_final_answer_transport

        content = unwrap_final_answer_transport(
            str(response.choices[0].message.content or "")
        )
        return AgentFinishAction(final_thought=content)

    AgentController._handle_action = handle_action
    AgentController._step = controller_step
    AgentController.set_agent_state_to = set_agent_state_to
    AgentController._is_stuck = is_stuck
    CodeActAgent.step = agent_step
    AgentController._fine_trace_overlay_installed = True


def main() -> None:
    install_fine_trace_overlay()
    runpy.run_module("openhands.core.main", run_name="__main__")


if __name__ == "__main__":
    main()
