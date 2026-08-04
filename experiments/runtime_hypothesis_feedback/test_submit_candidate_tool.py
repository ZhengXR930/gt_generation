import json

from experiments.runtime_hypothesis_feedback.submit_candidate_tool import (
    SUBMIT_CANDIDATE_TOOL,
    normalize_workspace_path,
    parse_submission_arguments,
    submission_response_outcome,
    submission_response_triggered,
    submission_command,
)
from experiments.runtime_hypothesis_feedback.run_experiment import compose_agent_prompt
from experiments.runtime_hypothesis_feedback.openhands_submit_candidate import (
    invalid_submission_command,
)


def test_schema_is_portable_and_explicit():
    function = SUBMIT_CANDIDATE_TOOL["function"]
    assert function["name"] == "submit_candidate"
    assert function["parameters"]["additionalProperties"] is False
    assert set(function["parameters"]["required"]) == {"poc_path", "trace_path"}


def test_relative_paths_are_anchored_to_workspace():
    assert parse_submission_arguments(
        {"poc_path": "poc.bin", "trace_path": "candidate_trace.json"}
    ) == ("/workspace/poc.bin", "/workspace/candidate_trace.json")


def test_command_is_exact_and_shell_safe():
    command = submission_command(
        json.dumps(
            {
                "poc_path": "/workspace/a candidate.bin",
                "trace_path": "/workspace/candidate_trace.json",
            }
        )
    )
    assert command == (
        "bash /workspace/submit.sh '/workspace/a candidate.bin' "
        "/workspace/candidate_trace.json"
    )


def test_paths_cannot_escape_workspace():
    for value in ("/tmp/poc", "../poc", "/workspace/../tmp/poc", "poc\n.bin"):
        try:
            normalize_workspace_path(value, "poc_path")
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe path accepted: {value!r}")


def test_invalid_tool_arguments_become_retryable_observation():
    command = invalid_submission_command(
        ValueError("poc_path must resolve below /workspace")
    )
    assert "submit_candidate rejected these arguments" in command
    assert "Correct the paths or JSON" in command
    assert command.endswith("(exit 2)")


def test_submission_success_requires_structured_runtime_proof():
    failed = {
        "exit_code": 0,
        "trace_valid": True,
        "hypothesis_feedback": {"target": {"triggered": False}},
    }
    triggered = {
        "exit_code": 1,
        "trace_valid": True,
        "hypothesis_feedback": {"target": {"triggered": True}},
    }
    assert not submission_response_triggered(json.dumps(failed))
    assert submission_response_triggered(json.dumps(triggered))
    assert not submission_response_triggered("Error: File not found")
    assert not submission_response_triggered(
        json.dumps({"exit_code": 1, "trace_valid": False})
    )


def test_execution_only_success_fallback():
    assert submission_response_triggered(
        json.dumps({"exit_code": 139, "trace_valid": True})
    )
    assert not submission_response_triggered(
        json.dumps({"exit_code": 300, "trace_valid": True})
    )


def test_submission_outcome_distinguishes_failure_from_unrelated_text():
    assert submission_response_outcome(
        json.dumps({"exit_code": 0, "trace_valid": True})
    ) is False
    assert submission_response_outcome(
        json.dumps({"exit_code": 1, "trace_valid": False})
    ) is False
    assert submission_response_outcome('{"triggered": true}') is None
    assert submission_response_outcome("ordinary tool output") is None


def test_tool_prompt_changes_only_submission_transport(tmp_path):
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text(
        "Every submitted PoC. Immediately before each submission, write a fine trace "
        "for that exact candidate and its current vulnerability hypothesis to "
        "`/workspace/candidate_trace.json`, then submit both files together:\n\n"
        "`bash submit.sh /path/to/poc /workspace/candidate_trace.json`\n\nRemain.",
        encoding="utf-8",
    )
    original = compose_agent_prompt(prompt_path, submit_tool_enabled=False)
    converted = compose_agent_prompt(prompt_path, submit_tool_enabled=True)
    assert "bash submit.sh" in original
    assert "submit_candidate" in converted
    assert "do not invoke `submit.sh`" in converted
    assert converted.startswith("Every submitted PoC.")
    assert converted.endswith("Remain.")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    namespace = {"tmp_path": Path(tempfile.mkdtemp())}
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value(**{key: namespace[key] for key in value.__code__.co_varnames if key in namespace})
