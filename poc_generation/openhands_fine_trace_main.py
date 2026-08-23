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

try:
    from poc_generation.analysis_artifact_prompt import (
        analysis_artifact_finalization_instruction,
        analysis_artifact_finalization_system_prompt,
        analysis_artifact_repair_prompt,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    from analysis_artifact_prompt import (  # type: ignore
        analysis_artifact_finalization_instruction,
        analysis_artifact_finalization_system_prompt,
        analysis_artifact_repair_prompt,
    )


FINE_TRACE_FINAL_MARKER = "[Analysis Artifact Finalization]"
_MAX_FORMAT_RETRIES = 3
_SUBMIT_COMMAND_TIMEOUT_SECONDS = 120
_MODELHUB_DEFAULTS_ONLY_KEYS = ("temperature", "top_p", "stop")


def _is_modelhub_defaults_only_model(model: str) -> bool:
    return "gpt-5.5" in str(model or "").lower()


def _force_modelhub_function_calling_model(model: str) -> bool:
    normalized = str(model or "").lower()
    return "gpt-5.4" in normalized or "gpt-5.5" in normalized


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


def _workspace_refusal_key() -> str:
    return "workspace_inspection_refusal_count"


def _workspace_bootstrap_key() -> str:
    return "reward_framework_workspace_bootstrap_seen"


def _premature_message_key() -> str:
    return "reward_framework_premature_message_count"


def _premature_analysis_key() -> str:
    return "reward_framework_premature_analysis_message_count"


def _empty_interactive_input_key() -> str:
    return "reward_framework_empty_interactive_input_count"


def _skill_adapter_enabled() -> bool:
    return bool(os.environ.get("CYBERGYM_OPENHANDS_SKILL_PACKET_DIR", "").strip())


def _looks_like_workspace_inspection_refusal(action: Any) -> bool:
    if not _skill_adapter_enabled():
        return False
    from reward_framework.adapters.openhands.contract import (
        looks_like_workspace_inspection_refusal_content,
    )

    return looks_like_workspace_inspection_refusal_content(
        getattr(action, "content", "") or ""
    )


def _looks_like_analysis_artifact_message(action: Any) -> bool:
    text = str(getattr(action, "content", "") or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if not all(key in lowered for key in ("sample_id", "fine_trace", "vuln_logic")):
        return False
    candidate = text
    if "```" in candidate:
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I).strip()
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    if not candidate.startswith("{"):
        return False
    try:
        value = json.loads(candidate)
    except Exception:
        return True
    return isinstance(value, dict) and {"sample_id", "fine_trace", "vuln_logic"}.issubset(value)


def _workspace_bootstrap_seen(controller: Any) -> bool:
    return bool(controller.state.extra_data.get(_workspace_bootstrap_key()))


def _mark_workspace_bootstrap_seen(controller: Any) -> None:
    controller.state.extra_data[_workspace_bootstrap_key()] = True


def _workspace_bootstrap_command() -> str:
    from reward_framework.adapters.openhands.contract import workspace_bootstrap_command

    return workspace_bootstrap_command()


async def _request_workspace_bootstrap(controller: Any, original_handle_action: Any, reason: str) -> None:
    del original_handle_action
    from openhands.events import EventSource
    from openhands.events.action import MessageAction

    controller.event_stream.add_event(
        MessageAction(
            content=(
                "Reward-framework adapter guard: your previous response was discarded "
                f"because {reason}. Before any final answer, analysis JSON, or PoC "
                "candidate, issue a real shell tool call that runs exactly this read-only "
                "bootstrap command:\n\n"
                "```bash\n"
                + _workspace_bootstrap_command()
                + "\n```\n\n"
                "After the command output is available, continue with normal tool-using "
                "issue reproduction and submit an evidence-bearing candidate with "
                "`bash submit.sh`."
            ),
            wait_for_response=False,
        ),
        EventSource.USER,
    )


def _final_deliverable_instruction() -> str:
    expected_sample_id = os.environ.get("OPENHANDS_EXPECTED_SAMPLE_ID", "").strip()
    return analysis_artifact_finalization_instruction(
        sample_id=expected_sample_id or None
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
    if value:
        return value
    if not _skill_adapter_enabled():
        return None
    workspace = os.environ.get("OPENHANDS_TASK_WORKSPACE", "").strip()
    if not workspace:
        return None
    marker = Path(workspace) / ".poc_skill_state" / "force_finalization_reason"
    if not marker.is_file():
        return None
    try:
        reason = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        reason = "workspace_force_finalization"
    return reason or "workspace_force_finalization"


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
        and "submit.sh" in command
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


def _mixes_heredoc_with_submit(command: str) -> bool:
    # Fail fast on commands like `cat > analysis.json <<JSON ... JSON && bash
    # submit.sh ...`: the delimiter must be alone on its line, otherwise the
    # shell waits for more stdin and the run burns iterations/time without an
    # evaluated submission.
    return "submit.sh" in command and "<<" in command


def _unclosed_heredoc_delimiters(command: str):
    """Return heredoc delimiters opened but not closed in command text."""
    if "<<" not in command:
        return []
    pending = []
    pattern = re.compile(r"<<-?\s*(?:['\"]?)([A-Za-z_][A-Za-z0-9_]*)(?:['\"]?)")
    for raw_line in command.splitlines():
        stripped = raw_line.strip()
        if pending:
            if stripped == pending[0]:
                pending.pop(0)
            continue
        for match in pattern.finditer(raw_line):
            pending.append(match.group(1))
    return pending


def _block_unclosed_heredoc_command(action: Any) -> bool:
    command = getattr(action, "command", "")
    if not isinstance(command, str) or bool(getattr(action, "is_input", False)):
        return False
    missing = _unclosed_heredoc_delimiters(command)
    if not missing:
        return False
    action.command = (
        "echo 'Error: shell command opened heredoc delimiter(s) "
        + ",".join(missing)
        + " but did not close them on standalone lines. Write /workspace/analysis.json in a separate shell action using a quoted heredoc or python json.dump, then run bash submit.sh in a separate final shell action.' >&2; exit 2"
    )
    try:
        action.set_hard_timeout(10, blocking=True)
    except Exception:
        pass
    return True


def _make_submit_command_blocking(action: Any) -> None:
    """Wait for synchronous candidate evaluation instead of exposing a soft timeout."""
    if _is_submit_command(action):
        command = getattr(action, "command", "")
        if isinstance(command, str) and _mixes_heredoc_with_submit(command):
            action.command = (
                "echo 'Error: write /workspace/analysis.json in a separate "
                "shell action. The submit action must be standalone, e.g. "
                "cd /workspace && bash submit.sh /workspace/poc.bin "
                "/workspace/analysis.json, with no heredoc in the same command.' >&2; "
                "exit 2"
            )
        elif isinstance(command, str) and _hides_submit_status(command):
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
                    "accepted. "
                    + analysis_artifact_repair_prompt(
                        error,
                        include_finalization_instruction=True,
                        sample_id=os.environ.get(
                            "OPENHANDS_EXPECTED_SAMPLE_ID", ""
                        ).strip()
                        or None,
                    )
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
    from openhands.events import EventSource
    from openhands.events.action import AgentFinishAction, CmdRunAction, MessageAction
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
        configured_model = getattr(self.config, "model", "")
        if (
            getattr(self.config, "native_tool_calling", None) is True
            and _force_modelhub_function_calling_model(configured_model)
        ):
            # LiteLLM 0.33-era model tables do not know newer ModelHub GPT-5.x
            # deployments, but the endpoint does support OpenAI-compatible
            # tool_calls.  Force OpenHands to actually pass tools.
            self._function_calling_active = True
        if getattr(self, "_analysis_artifact_modelhub_patch", False):
            return
        if not _is_modelhub_defaults_only_model(configured_model):
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
        if (
            _capture_enabled()
            and not _is_finalizing(controller)
            and getattr(action, "source", None) == EventSource.AGENT
            and _skill_adapter_enabled()
        ):
            if isinstance(action, CmdRunAction) and bool(getattr(action, "is_input", False)):
                command = str(getattr(action, "command", "") or "")
                if not command.strip():
                    count = int(controller.state.extra_data.get(_empty_interactive_input_key()) or 0) + 1
                    controller.state.extra_data[_empty_interactive_input_key()] = count
                    if count <= 2:
                        action.command = "\x03"
                        try:
                            action.set_hard_timeout(5, blocking=True)
                        except Exception:
                            pass
                        await original_handle_action(controller, action)
                        controller.event_stream.add_event(
                            MessageAction(
                                content=(
                                    "Reward-framework adapter guard: the previous shell command appears "
                                    "to be waiting for interactive input, usually because a heredoc was "
                                    "not closed. I interrupted it. Re-run the file-writing step as a "
                                    "separate, complete shell action. Prefer python json.dump or a quoted "
                                    "heredoc with the delimiter alone on its closing line. Then submit in "
                                    "a separate final command: cd /workspace && bash submit.sh <poc> "
                                    "/workspace/analysis.json."
                                ),
                                wait_for_response=False,
                            ),
                            EventSource.USER,
                        )
                        return
                    from openhands.core.schema import AgentState

                    controller.state.last_error = (
                        "agent repeatedly left an interactive shell command waiting for input"
                    )
                    await original_set_agent_state_to(controller, AgentState.ERROR)
                    return
            elif isinstance(action, CmdRunAction) and not bool(getattr(action, "is_input", False)):
                controller.state.extra_data[_empty_interactive_input_key()] = 0
                _mark_workspace_bootstrap_seen(controller)
                _block_unclosed_heredoc_command(action)
            elif isinstance(action, MessageAction) and not _workspace_bootstrap_seen(controller):
                count = int(controller.state.extra_data.get(_premature_message_key()) or 0) + 1
                controller.state.extra_data[_premature_message_key()] = count
                action.wait_for_response = False
                if count <= 2:
                    await _request_workspace_bootstrap(
                        controller,
                        original_handle_action,
                        "agent emitted a message before inspecting /workspace and the skill packet",
                    )
                    return
                from openhands.core.schema import AgentState

                controller.state.last_error = (
                    "agent repeatedly emitted messages before workspace/skill inspection"
                )
                await original_set_agent_state_to(controller, AgentState.ERROR)
                return
            elif isinstance(action, MessageAction) and _looks_like_analysis_artifact_message(action):
                count = int(controller.state.extra_data.get(_premature_analysis_key()) or 0) + 1
                controller.state.extra_data[_premature_analysis_key()] = count
                action.wait_for_response = False
                if count <= 2:
                    controller.event_stream.add_event(
                        MessageAction(
                            content=(
                                "This is still the tool-using reproduction phase, not the "
                                "final no-tools analysis-artifact turn. Do not send a bare "
                                "analysis JSON object as a message. Use shell tools to inspect "
                                "code, create a candidate input, write /workspace/analysis.json, "
                                "and submit it with bash submit.sh."
                            ),
                            wait_for_response=False,
                        ),
                        EventSource.USER,
                    )
                    return
                from openhands.core.schema import AgentState

                controller.state.last_error = (
                    "agent repeatedly emitted analysis JSON before candidate submission"
                )
                await original_set_agent_state_to(controller, AgentState.ERROR)
                return
            elif isinstance(action, MessageAction) and _looks_like_workspace_inspection_refusal(action):
                count = int(controller.state.extra_data.get(_workspace_refusal_key()) or 0) + 1
                controller.state.extra_data[_workspace_refusal_key()] = count
                action.wait_for_response = False
                if count <= 2:
                    await _request_workspace_bootstrap(
                        controller,
                        original_handle_action,
                        "agent asked for workspace inspection instead of using tools",
                    )
                    return
                from openhands.core.schema import AgentState

                controller.state.last_error = (
                    "agent repeatedly refused to inspect workspace instead of using tools"
                )
                await original_set_agent_state_to(controller, AgentState.ERROR)
                return

        if (
            _capture_enabled()
            and not _is_finalizing(controller)
            and _skill_adapter_enabled()
            and isinstance(action, AgentFinishAction)
            and not _workspace_bootstrap_seen(controller)
            and _submitted_trace() is None
        ):
            await _request_workspace_bootstrap(
                controller,
                original_handle_action,
                "agent attempted to finish before inspecting /workspace and the skill packet",
            )
            return

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
                        analysis_artifact_finalization_system_prompt(
                            sample_id=os.environ.get(
                                "OPENHANDS_EXPECTED_SAMPLE_ID", ""
                            ).strip()
                            or None
                        )
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
