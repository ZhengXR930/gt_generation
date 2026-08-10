import json
from collections import deque
from types import SimpleNamespace

from poc_generation import openhands_fine_trace_main as overlay


def test_iteration_limit_checkpoints_before_finalization(tmp_path, monkeypatch):
    calls = []
    controller = SimpleNamespace(
        state=SimpleNamespace(
            iteration=100,
            max_iterations=100,
            extra_data={},
        ),
        agent=SimpleNamespace(pending_actions=deque()),
        _pending_action=object(),
        event_stream=SimpleNamespace(add_event=lambda action, source: calls.append("marker")),
    )
    monkeypatch.setenv("OPENHANDS_HARNESS_MODE", "evaluation")
    monkeypatch.setenv("OPENHANDS_CAPTURE_FINE_TRACE", "1")
    monkeypatch.setattr(
        overlay,
        "_write_pre_finalization_checkpoint",
        lambda value: calls.append("checkpoint") or tmp_path / "checkpoint",
    )

    assert overlay._start_finalization(controller, "iteration_limit")
    assert calls == ["checkpoint", "marker"]
    assert controller._pending_action is None
    assert controller.state.extra_data["fine_trace_finalization"] == {
        "status": "answering",
        "trigger": "iteration_limit",
        "tool_access": "disabled",
        "started_iteration": 100,
        "pre_finalization_checkpoint": str(tmp_path / "checkpoint"),
    }


def test_agent_finish_checkpoints_before_finalization(tmp_path, monkeypatch):
    calls = []
    controller = SimpleNamespace(
        state=SimpleNamespace(
            iteration=17,
            max_iterations=100,
            extra_data={},
        ),
        agent=SimpleNamespace(pending_actions=deque()),
        _pending_action=object(),
        event_stream=SimpleNamespace(add_event=lambda action, source: calls.append("marker")),
    )
    monkeypatch.setenv("OPENHANDS_HARNESS_MODE", "evaluation")
    monkeypatch.setenv("OPENHANDS_CAPTURE_FINE_TRACE", "1")
    monkeypatch.setattr(
        overlay,
        "_write_pre_finalization_checkpoint",
        lambda value: calls.append("checkpoint") or tmp_path / "checkpoint",
    )

    assert overlay._start_finalization(controller, "agent_finished")
    assert calls == ["checkpoint", "marker"]
    assert controller.state.extra_data["fine_trace_finalization"][
        "pre_finalization_checkpoint"
    ] == str(tmp_path / "checkpoint")


def test_submit_command_is_forced_to_block_until_server_response():
    class Action:
        command = "cd /workspace && bash submit.sh /workspace/poc /workspace/trace.json"
        is_input = False
        blocking = False

        def set_hard_timeout(self, value, blocking=True):
            self.timeout = value
            self.blocking = blocking

    action = Action()
    overlay._make_submit_command_blocking(action)

    assert action.timeout == 120
    assert action.blocking is True


def test_submit_command_input_is_not_rewritten():
    class Action:
        command = "C-c"
        is_input = True
        blocking = False

        def set_hard_timeout(self, value, blocking=True):
            raise AssertionError("interactive input must not be rewritten")

    overlay._make_submit_command_blocking(Action())


def test_message_builder_supports_pristine_and_evolved_harness_contracts():
    class Pristine:
        def _get_messages(self, events):
            return ("pristine", events)

    class Evolved:
        def _get_messages(self, events, state):
            return ("evolved", events, state)

    events = ["event"]
    state = object()
    assert overlay._get_agent_messages(Pristine(), events, state) == (
        "pristine", events
    )
    assert overlay._get_agent_messages(Evolved(), events, state) == (
        "evolved", events, state
    )


def test_pre_finalization_checkpoint_captures_state_cache_and_trajectory(
    tmp_path, monkeypatch
):
    file_root = tmp_path / "source-file"
    cache_root = tmp_path / "source-cache"
    file_root.mkdir()
    cache_root.mkdir()
    (cache_root / "cache.bin").write_text("cache", encoding="utf-8")

    class State:
        iteration = 100
        max_iterations = 100
        history = []

        def save_to_session(self, *args):
            (file_root / "saved").write_text("yes", encoding="utf-8")

    controller = SimpleNamespace(
        state=State(),
        event_stream=SimpleNamespace(
            sid="sid",
            user_id="user",
            file_store=SimpleNamespace(root=str(file_root)),
        ),
    )
    destination = tmp_path / "checkpoint"
    monkeypatch.setenv(
        "OPENHANDS_PRE_FINALIZATION_CHECKPOINT", str(destination)
    )
    monkeypatch.setenv("OPENHANDS_CACHE_DIR", str(cache_root))

    assert overlay._write_pre_finalization_checkpoint(controller) == destination
    assert (destination / "file" / "saved").read_text(encoding="utf-8") == "yes"
    assert (destination / "cache" / "cache.bin").read_text(encoding="utf-8") == "cache"
    assert json.loads((destination / "trajectory").read_text(encoding="utf-8")) == []
    metadata = json.loads(
        (destination / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["phase"] == "pre_fine_trace_finalization"
    assert metadata["iteration"] == 100
