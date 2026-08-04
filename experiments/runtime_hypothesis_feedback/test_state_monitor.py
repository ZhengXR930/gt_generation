import json

from experiments.runtime_hypothesis_feedback.state_monitor import (
    CandidateBootstrapMachine,
    Phase,
    is_submission_action,
)


def run_event(event_id: int, command: str) -> dict:
    return {
        "id": event_id,
        "source": "agent",
        "action": "run",
        "args": {"command": command, "thought": "inspect the input interface"},
    }


def test_llm_readiness_injects_bootstrap(tmp_path):
    injected = []

    def ready(*args, **kwargs):
        return {
            "readiness": "ready",
            "stalled_in_analysis": False,
            "evidence_event_ids": [1, 2],
            "reason": "harness and input layout were observed",
        }

    machine = CandidateBootstrapMachine(
        skeleton={},
        log_path=tmp_path / "monitor.jsonl",
        api_key="test",
        inject_message=injected.append,
        judge=ready,
        check_interval=2,
        orientation_ceiling=10,
    )
    machine.observe(run_event(1, "cat README.md"))
    machine.observe(run_event(2, "cat fuzzer.cc"))
    assert machine.phase == Phase.BOOTSTRAP_REQUIRED
    assert len(injected) == 1
    log = [json.loads(line) for line in machine.log_path.read_text().splitlines()]
    assert any(item["kind"] == "monitor_decision" for item in log)


def test_submission_transition_is_deterministic(tmp_path):
    machine = CandidateBootstrapMachine(
        skeleton={},
        log_path=tmp_path / "monitor.jsonl",
        api_key="test",
        inject_message=lambda message: None,
        judge=lambda *args, **kwargs: {},
    )
    event = run_event(1, "cd /workspace && bash submit.sh /tmp/poc candidate_trace.json")
    assert is_submission_action(event)
    machine.observe(event)
    assert machine.phase == Phase.FEEDBACK_LOOP


def test_native_submission_tool_metadata_is_authoritative():
    event = {
        "source": "agent",
        "action": "run",
        "args": {"command": "opaque platform adapter command"},
        "tool_call_metadata": {"function_name": "submit_candidate"},
    }
    assert is_submission_action(event)


def test_pending_bootstrap_repeats_even_when_agent_is_progressing(tmp_path):
    injected = []
    calls = []

    def judge(*args, **kwargs):
        calls.append(1)
        return {
            "readiness": "ready",
            "stalled_in_analysis": False,
            "evidence_event_ids": [1],
            "reason": "agent is progressing but has not submitted",
        }

    machine = CandidateBootstrapMachine(
        skeleton={},
        log_path=tmp_path / "monitor.jsonl",
        api_key="test",
        inject_message=injected.append,
        judge=judge,
        check_interval=1,
        repeat_interval=2,
    )
    machine.observe(run_event(1, "cat harness.cc"))
    machine.observe(run_event(2, "inspect source"))
    machine.observe(run_event(3, "write candidate"))
    assert machine.phase == Phase.BOOTSTRAP_REQUIRED
    assert len(injected) == 2
    assert len(calls) == 1


def test_reading_submit_script_is_not_a_submission():
    assert not is_submission_action(run_event(1, "cat /workspace/submit.sh"))
