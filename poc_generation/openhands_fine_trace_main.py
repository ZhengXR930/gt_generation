"""OpenHands entrypoint that adds the evaluation analysis-artifact final turn.

The upstream OpenHands checkout stays pinned and unmodified.  This module
installs the small evaluation-specific lifecycle overlay before delegating to
``openhands.core.main``:

* persist the last analysis.json artifact submitted with a candidate input when one exists;
* at the iteration limit, checkpoint before asking for the final artifact;
* run the final artifact turn without tools; and
* validate and persist the resulting bare JSON object.
"""

from __future__ import annotations

import json
import inspect
import os
import re
import runpy
import shutil
import tempfile
from functools import partial
from pathlib import Path
from typing import Any


FINE_TRACE_FINAL_MARKER = "[Analysis Artifact Finalization]"
_MAX_FORMAT_RETRIES = 3
_SUBMIT_COMMAND_TIMEOUT_SECONDS = 120
_REWARD_SUBMIT_COMMAND = re.compile(
    r"(?:^|(?:&&|\|\||;|\n)\s*)"
    r"(?:python3\s+)?/workspace/\.reward_framework/submit_candidate\.py"
    r"(?:\s|$)"
)
_MODELHUB_DEFAULTS_ONLY_KEYS = ("temperature", "top_p", "stop")


def _is_modelhub_defaults_only_model(model: str) -> bool:
    return "gpt-5.5" in str(model or "").lower()


def _drop_modelhub_defaults_only_params(kwargs: dict[str, Any], model: str) -> None:
    if not _is_modelhub_defaults_only_model(model):
        return
    for key in _MODELHUB_DEFAULTS_ONLY_KEYS:
        kwargs.pop(key, None)


def _capture_enabled() -> bool:
    return (
        os.environ.get("OPENHANDS_HARNESS_MODE", "evaluation") == "evaluation"
        and os.environ.get("OPENHANDS_CAPTURE_FINE_TRACE", "0") == "1"
    )


def _finalization_key() -> str:
    return "analysis_artifact_finalization"


def _force_started_key() -> str:
    return "analysis_artifact_force_started"


def _final_deliverable_instruction() -> str:
    expected_sample_id = os.environ.get("OPENHANDS_EXPECTED_SAMPLE_ID", "").strip()
    sample_id_instruction = (
        f"sample_id must be exactly {expected_sample_id!r}; do not rewrite "
        "separators or dataset prefixes. "
        if expected_sample_id
        else "sample_id is the current benchmark sample id from the task metadata "
        "or workspace prompt, and must not be empty. "
    )
    return (
        "Return ONLY one bare JSON object with exactly three top-level keys: "
        "sample_id, fine_trace and vuln_logic. Do not emit Markdown, prose, XML, "
        "DSML, tool calls, confidence fields, GT identifiers, or trace_step references. "
        + sample_id_instruction
        +
        "Use this JSON shape: "
        '{"sample_id":"exact_sample_id","fine_trace":[{"step":1,"file":"project/source/file.c",'
        '"function":"function_name","line":123,"var":"source_expr","code":"source statement",'
        '"role":"source","note":"why this step matters"}],"vuln_logic":{"source":'
        '{"file":"same as source trace step","function":"same function","line":123,'
        '"operands":["input_controlled_expr"]},"root_cause":{"file":"same as '
        'root_cause trace step","function":"same function","line":130,"operands":'
        '["left_expr","right_expr"],"relation":{"op":"lt","left":"left_expr",'
        '"right":"right_expr"}},"sink":{"file":"same as sink trace step","function":'
        '"same function","line":140,"operands":["left_expr","right_expr"],"relation":'
        '{"op":"gt","left":"left_expr","right":"right_expr"}},"propagation":[{"from":'
        '{"file":"file.c","function":"f","line":123,"operands":["expr"]},"to":'
        '{"file":"file.c","function":"f","line":140,"operands":["expr"]},"type":"data",'
        '"via":["expr"],"relation":{"op":"eq","left":"expr","right":"expr"}}]}}. '
        "Field meanings: fine_trace is the shortest sufficient causal path through "
        "project source code under the local benchmark input. Omit harness boilerplate, setup, generic "
        "parser admission, README/workspace artifacts, runtime logs, and incidental "
        "exploration. fine_trace.step is an integer starting at 1 in causal/execution "
        "order. fine_trace.file/function/line is a project source location; "
        "any step used by vuln_logic must have an integer line. fine_trace.var is one "
        "concrete source expression, variable, field, macro, literal, or language-native "
        "variable token at that step. fine_trace.role is one of source, root_cause, "
        "sink, intermediate, or null. There must be exactly one source step, one "
        "root_cause step, and one sink step. Do not output depends_on. Role meanings: "
        "source is the first project source statement where input-controlled "
        "data or bug-relevant state becomes a program value used by the real "
        "implementation, not harness/test/README/workspace setup. root_cause is the "
        "project source statement that represents the missing or violated safety "
        "obligation: pointer must be NULL after transfer, index < capacity, remaining "
        "bytes >= read size, object alive before use, buffer initialized before read, "
        "etc. It is not a symptom, crash line, generic error check, or harness line. "
        "sink is the project source statement where the target invalid operation or "
        "bug manifestation happens. intermediate is a project source statement needed to "
        "carry data, control, object identity, lifetime, size, or ordering. vuln_logic "
        "is a projection from role-marked fine_trace steps, not a second independent "
        "story. source, root_cause, and sink must copy file/function/line from the "
        "single fine_trace step with that role. source has operands only and no "
        "relation or op. root_cause and sink require relation exactly {op,left,right}. "
        "root_cause.relation states the safety condition that should have held to "
        "avoid the bug, not the vulnerable-path negation; for example, if the bug "
        "happens because i >= capacity, write root_cause.relation as lt(i,capacity). "
        "source/root_cause/sink operands and relation terms must be grounded in the "
        "same fine_trace step marked with that role; if vuln_logic.sink talks about "
        "glyph_props, the fine_trace sink step must also involve glyph_props. "
        "propagation edges connect existing fine_trace steps; from and to must copy "
        "file/function/line from existing trace steps. propagation.type is data, "
        "control, or order. propagation.via is the carrier expression, guard expression, "
        "or order keyword. propagation.relation is optional and, when present, is "
        "exactly {op,left,right}. relation.op is eq, ne, lt, le, gt, ge, or same_object. "
        "Keep left/right direction meaningful for ordered relations. root_cause.relation "
        "must be the real safety condition, while sink.relation must be the target "
        "operation's required or violated sink predicate; "
        "do not use tautologies such as eq(x,x) or same_object(x,x) to fill the field. "
        "operands, via, "
        "relation.left, and relation.right must be concrete verbatim source expressions "
        "or literals from the cited source evidence, not prose labels, English explanatory "
        "phrases, invented property names, or placeholders such as $event.field. README.md, "
        "workspace, checkpoint files, analysis.json, prompts, runtime logs, harness, test, "
        "and fuzz setup code are not valid anchors for source, root_cause, sink, or "
        "propagation endpoints."
    )


def _validate_final_response(response: str) -> str | None:
    from evaluator.reasoning.analysis_artifact import (
        parse_analysis_artifact,
        validate_analysis_artifact,
        validate_analysis_artifact_quality,
    )

    error = validate_analysis_artifact(response)
    if error:
        return error
    expected_sample_id = os.environ.get("OPENHANDS_EXPECTED_SAMPLE_ID", "").strip()
    if expected_sample_id:
        artifact = parse_analysis_artifact(response)
        if artifact is None or artifact.get("sample_id") != expected_sample_id:
            return (
                "sample_id must exactly equal "
                f"{expected_sample_id!r}; do not rewrite separators or dataset prefixes"
            )
    return validate_analysis_artifact_quality(response)


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


def _hides_submit_status(command: str) -> bool:
    submit_index = command.find("submit.sh")
    if submit_index < 0:
        return False
    before = command[:submit_index]
    after = command[submit_index:]
    return (
        "set +e" in before
        or "||" in after
        or re.search(r";\s*(?:echo\b|true\b|exit\s+0\b)", after) is not None
    )


def _make_submit_command_blocking(action: Any) -> None:
    """Wait for synchronous candidate evaluation instead of exposing a soft timeout."""
    if _is_submit_command(action):
        command = getattr(action, "command", "")
        if isinstance(command, str) and _hides_submit_status(command):
            action.command = (
                "echo 'Error: submit.sh must be the final command in this "
                "shell action; do not hide or overwrite its exit status.' >&2; "
                "exit 2"
            )
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
    default_trace = str(Path(workspace) / ".latest_analysis.json") if workspace else ""
    marker = os.environ.get(
        "OPENHANDS_POC_SUBMISSION_MARKER", default_marker
    ).strip()
    trace = os.environ.get(
        "OPENHANDS_LATEST_SUBMISSION_ANALYSIS", default_trace
    ).strip()
    if not marker or not trace or not Path(marker).is_file() or not Path(trace).is_file():
        return None
    try:
        response = Path(trace).read_text(encoding="utf-8")
        return response if _validate_final_response(response) is None else None
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
                    "phase": "pre_analysis_artifact_finalization",
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
                f"{FINE_TRACE_FINAL_MARKER} The local benchmark task has ended because: "
                f"{trigger}. {_final_deliverable_instruction()}"
            ),
            wait_for_response=False,
        ),
        EventSource.USER,
    )
    return True


async def _persist_trace(controller: Any, response: str, trigger: str) -> None:
    from openhands.core.schema import AgentState

    from evaluator.reasoning.analysis_artifact import write_analysis_artifact

    output = Path(
        os.environ.get("OPENHANDS_ANALYSIS_OUTPUT")
        or os.environ.get("OPENHANDS_ANALYSIS_ARTIFACT_OUTPUT")
        or Path(os.environ.get("LOG_DIR") or ".") / "analysis.json"
    )
    try:
        error = _validate_final_response(response)
        if error is not None:
            raise ValueError(error)
        write_analysis_artifact(output, response)
        result = {
            "status": "completed",
            "trigger": trigger,
            "output": str(output),
            "analysis_output": str(output),
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
    if error is not None and attempts < _MAX_FORMAT_RETRIES:
        controller.event_stream.add_event(
            MessageAction(
                content=(
                    f"{FINE_TRACE_FINAL_MARKER} The final deliverable was not "
                    f"accepted: {error}. Return only a corrected bare JSON object "
                    "with exactly sample_id, fine_trace, and vuln_logic. If the "
                    "error names operands, via, relation.left, or relation.right, "
                    "replace that field with a concrete source expression, literal, "
                    "macro, or function-call expression from the cited source evidence. "
                    "Do not use English explanatory phrases. If the error mentions "
                    "harness, test, fuzz, README, or workspace, remove that key role "
                    "and choose the first real project source statement "
                    "for source, the violated safety-obligation statement for "
                    "root_cause, and the target invalid operation statement for sink. If "
                    "the error says relation is tautological or must describe the "
                    "violated safety condition, replace eq(x,x) or same_object(x,x) "
                    "with the actual required predicate from the project source, "
                    "such as index < capacity or object != NULL before use. If "
                    "the error says operands/relation must be grounded in the same "
                    "fine_trace step, either move that role to the trace step that "
                    "actually contains those expressions or change vuln_logic to "
                    "use expressions from the current role step. "
                    f"{_final_deliverable_instruction()}"
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
    from openhands.llm.llm import LLM
    from openhands.memory.condenser.condenser import Condensation, View

    if getattr(AgentController, "_fine_trace_overlay_installed", False):
        return

    original_handle_action = AgentController._handle_action
    original_step = AgentController._step
    original_agent_step = CodeActAgent.step
    original_set_agent_state_to = AgentController.set_agent_state_to
    original_is_stuck = AgentController._is_stuck
    original_llm_init = LLM.__init__

    def llm_init(self, *args, **kwargs):
        original_llm_init(self, *args, **kwargs)
        if getattr(self, "_analysis_artifact_modelhub_patch", False):
            return
        if not _is_modelhub_defaults_only_model(getattr(self.config, "model", "")):
            return
        original_completion = self._completion_unwrapped
        if isinstance(original_completion, partial):
            bound_kwargs = dict(original_completion.keywords or {})
            _drop_modelhub_defaults_only_params(bound_kwargs, self.config.model)
            original_completion = partial(
                original_completion.func,
                *original_completion.args,
                **bound_kwargs,
            )

        def completion_without_unsupported_defaults(*call_args, **call_kwargs):
            _drop_modelhub_defaults_only_params(call_kwargs, self.config.model)
            return original_completion(*call_args, **call_kwargs)

        self._completion_unwrapped = completion_without_unsupported_defaults
        self._completion = completion_without_unsupported_defaults
        self.config.temperature = None
        self.config.top_p = None
        self._analysis_artifact_modelhub_patch = True

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
            and not _is_finalizing(controller)
        ):
            submitted = _submitted_trace()
            if submitted is not None:
                _write_pre_finalization_checkpoint(controller)
                await _persist_trace(controller, submitted, "last_poc_submission")
                return
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
    LLM.__init__ = llm_init
    AgentController._fine_trace_overlay_installed = True


def main() -> None:
    install_fine_trace_overlay()
    runpy.run_module("openhands.core.main", run_name="__main__")


if __name__ == "__main__":
    main()
