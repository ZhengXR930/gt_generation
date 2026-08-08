"""OpenHands adapter, native submission transport, and lifecycle hooks."""

from __future__ import annotations

import copy
import http.server
import json
import os
import re
import secrets
import shlex
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .base import CallbackAdapter
from ..state_store import atomic_json
from ..submission_tool import SUBMIT_CANDIDATE_TOOL, TOOL_NAME


class OpenHandsAdapter(CallbackAdapter):
    def __init__(self, *, workspace_root: Path,
                 inject: Callable[[str], None],
                 checkpoint_callback: Callable[[str], Path | None]):
        super().__init__(workspace_root=workspace_root, inject=inject,
                         checkpoint_callback=checkpoint_callback)

    def submission_ready(self) -> bool:
        """The protocol requires the exact candidate trace before submission."""
        return (self.workspace_root / "candidate_trace.json").is_file()

    @staticmethod
    def normalize_event(event: Any) -> tuple[str, str, dict[str, Any]]:
        if isinstance(event, dict):
            source = str(event.get("source") or "unknown")
            kind = str(event.get("action") or event.get("observation") or "event")
            return source, kind, dict(event)
        return "unknown", type(event).__name__, {"text": str(event)}


def install_closed_network_runtime_route() -> None:
    """Reach the action server through an internal Docker bridge address."""
    network = os.getenv("OPENHANDS_RUNTIME_DOCKER_NETWORK", "").strip()
    if not network:
        return
    from openhands.runtime.impl.docker.docker_runtime import DockerRuntime

    if getattr(DockerRuntime, "_reward_closed_network_route_installed", False):
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
    DockerRuntime._reward_closed_network_route_installed = True


def _workspace_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    path = PurePosixPath(value.strip())
    if not path.is_absolute():
        path = PurePosixPath("/workspace") / path
    if path.parts[:2] != ("/", "workspace") or ".." in path.parts:
        raise ValueError(f"{field} must resolve below /workspace")
    return str(path)


_CLIENT = r'''#!/usr/bin/env python3
import json, pathlib, shutil, sys, urllib.error, urllib.request, uuid

if len(sys.argv) != 3:
    raise SystemExit("usage: submit_candidate.py <poc> <trace>")
workspace = pathlib.Path("/workspace").resolve()
def safe(raw):
    path = pathlib.Path(raw)
    if not path.is_absolute():
        path = workspace / path
    path = path.resolve()
    if workspace not in path.parents or not path.is_file():
        raise SystemExit("submission path must be an existing file below /workspace")
    return path
poc, trace = safe(sys.argv[1]), safe(sys.argv[2])
attempt = uuid.uuid4().hex
target = workspace / ".reward_submissions" / attempt
target.mkdir(parents=True)
shutil.copy2(poc, target / "poc")
shutil.copy2(trace, target / "trace.json")
request = urllib.request.Request(
    __URL__ + "/submit",
    data=json.dumps({"token": __TOKEN__, "attempt_id": attempt}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(request, timeout=900) as response:
        print(response.read().decode())
except urllib.error.HTTPError as exc:
    print(exc.read().decode(), file=sys.stderr)
    raise SystemExit(3)
'''


class OpenHandsRewardTransport:
    """Bridge a sandboxed native tool call to the host-side framework."""

    def __init__(self, *, workspace_root: Path, framework: Any):
        self.workspace_root = workspace_root.resolve()
        self.framework = framework
        self.token = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._terminal_result: dict[str, Any] | None = None
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _reply(self, status: int, value: dict[str, Any]) -> None:
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                try:
                    if self.path != "/submit":
                        self._reply(404, {"error": "unknown endpoint"})
                        return
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > 16_384:
                        raise ValueError("invalid request size")
                    request = json.loads(self.rfile.read(length))
                    if not secrets.compare_digest(
                        str(request.get("token") or ""), bridge.token
                    ):
                        self._reply(403, {"error": "invalid token"})
                        return
                    attempt = str(request.get("attempt_id") or "")
                    if len(attempt) != 32 or any(c not in "0123456789abcdef" for c in attempt):
                        raise ValueError("invalid attempt id")
                    relative = Path(".reward_submissions") / attempt
                    with bridge._lock:
                        status = bridge.framework.status()
                        if (
                            status.get("terminal_reason") == "trigger_success"
                            and bridge._terminal_result is not None
                        ):
                            result = dict(bridge._terminal_result)
                            result["replayed_after_terminal"] = True
                        else:
                            result = bridge.framework.submit_candidate({
                                "poc_path": str(relative / "poc"),
                                "trace_path": str(relative / "trace.json"),
                            })
                            if result.get("triggered") is True:
                                bridge._terminal_result = dict(result)
                    self._reply(200, result)
                except Exception as exc:
                    self._reply(400, {"error": f"{type(exc).__name__}: {exc}"})

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        gateway = os.getenv("OPENHANDS_EVAL_HOST_GATEWAY", "172.17.0.1")
        return f"http://{gateway}:{self.server.server_port}"

    def start(self) -> None:
        self.thread.start()
        directory = self.workspace_root / ".reward_framework"
        directory.mkdir(parents=True, exist_ok=True)
        script = _CLIENT.replace("__URL__", repr(self.url)).replace(
            "__TOKEN__", repr(self.token)
        )
        path = directory / "submit_candidate.py"
        path.write_text(script, encoding="utf-8")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _submission_command(arguments: str | dict[str, Any]) -> str:
    value = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(value, dict) or set(value) != {"poc_path", "trace_path"}:
        raise ValueError("submit_candidate requires poc_path and trace_path")
    poc = _workspace_path(value["poc_path"], "poc_path")
    trace = _workspace_path(value["trace_path"], "trace_path")
    return " ".join(shlex.quote(x) for x in (
        "python3", "/workspace/.reward_framework/submit_candidate.py", poc, trace
    ))


_DIRECT_SUBMIT = re.compile(
    r"(?:^|(?:&&|\|\||;|\n)\s*)(?:bash\s+)?"
    r"(?:\./|/workspace/)?submit\.sh(?:\s|$)"
)


def is_direct_submit_invocation(command: str) -> bool:
    """Distinguish executing submit.sh from merely reading or mentioning it."""
    return bool(_DIRECT_SUBMIT.search(command.strip()))


def install_openhands_reward_framework(*, agent: Any, event_stream: Any,
                                       framework: Any) -> OpenHandsRewardTransport:
    """Install the real tool, trajectory observer, and terminal policy."""
    import openhands.agenthub.codeact_agent.function_calling as function_calling
    from openhands.events.action import AgentFinishAction, CmdRunAction, NullAction
    from openhands.events.serialization.event import event_to_dict

    if not any(tool["function"]["name"] == TOOL_NAME for tool in agent.tools):
        agent.tools.append(copy.deepcopy(SUBMIT_CANDIDATE_TOOL))
    transport = OpenHandsRewardTransport(
        workspace_root=framework.platform.workspace_root, framework=framework
    )
    transport.start()
    original_converter = function_calling.response_to_actions

    def response_to_actions(response):
        translated = copy.deepcopy(response)
        indexes: list[int] = []
        calls = getattr(translated.choices[0].message, "tool_calls", None) or []
        for index, call in enumerate(calls):
            if call.function.name != TOOL_NAME:
                continue
            command = _submission_command(call.function.arguments)
            call.function.name = "execute_bash"
            call.function.arguments = json.dumps({"command": command, "is_input": "false"})
            indexes.append(index)
        actions = original_converter(translated)
        for index in indexes:
            original = response.choices[0].message.tool_calls[index]
            calls[index].function.name = TOOL_NAME
            calls[index].function.arguments = original.function.arguments
            actions[index].tool_call_metadata.function_name = TOOL_NAME
            actions[index].set_hard_timeout(900, blocking=True)
        return actions

    function_calling.response_to_actions = response_to_actions
    original_step = agent.step
    seen_events = 0

    def controlled_step(state):
        nonlocal seen_events
        history = list(state.history)
        for event in history[seen_events:]:
            value = event_to_dict(event)
            source, kind, payload = OpenHandsAdapter.normalize_event(value)
            framework.record_event(source=source, kind=kind, payload=payload)
        saw_new_events = len(history) > seen_events
        seen_events = len(history)
        status = framework.status()
        if status["terminal_reason"] == "trigger_success":
            return AgentFinishAction(
                final_thought="The independent runtime oracle confirmed the trigger.",
                task_completed="true",
            )
        decision = "continue"
        if saw_new_events and not status["awaiting_verification"]:
            decision = framework.observe_trajectory()
        action = original_step(state)
        if isinstance(action, AgentFinishAction):
            allowed = framework.before_finish(
                iteration=int(getattr(state, "iteration", 0) or 0),
                maximum=int(getattr(state, "max_iterations", 100) or 100),
            )
            return action if allowed else NullAction()
        if isinstance(action, CmdRunAction) and is_direct_submit_invocation(
            str(action.command)
        ):
            action.command = (
                "printf '%s\\n' 'Direct submit.sh invocation is disabled. "
                "Use the first-class submit_candidate tool.' >&2; (exit 2)"
            )
            return action
        submission_required = (
            decision == "request_submission"
            or framework.status()["submission_requested"]
        )
        metadata = getattr(action, "tool_call_metadata", None)
        is_submission = bool(
            metadata and getattr(metadata, "function_name", None) == TOOL_NAME
        )
        if submission_required and not is_submission:
            # The injected observer message becomes visible on the next turn;
            # discard this stale proposal so it cannot race ahead of the
            # requested first-class submission boundary.
            return NullAction()
        return action

    agent.step = controlled_step
    agent._reward_framework_transport = transport
    return transport


def create_openhands_adapter(*, workspace_root: Path, event_stream: Any,
                             checkpoint_root: Path) -> OpenHandsAdapter:
    """Construct the adapter used before creating and binding the framework."""
    from openhands.events import EventSource
    from openhands.events.action import MessageAction
    from openhands.events.serialization.event import event_to_dict

    def inject(message: str) -> None:
        event_stream.add_event(
            MessageAction(content=message, wait_for_response=False), EventSource.USER
        )

    def checkpoint(label: str) -> Path:
        path = checkpoint_root.resolve() / label
        path.mkdir(parents=True, exist_ok=True)
        try:
            events = [event_to_dict(event) for event in event_stream.get_events()]
        except Exception as exc:
            events = [{"checkpoint_error": f"{type(exc).__name__}: {exc}"}]
        atomic_json(path / "trajectory.json", events)
        return path

    return OpenHandsAdapter(
        workspace_root=workspace_root, inject=inject,
        checkpoint_callback=checkpoint,
    )
