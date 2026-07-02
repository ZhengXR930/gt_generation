import json

from evaluator.poc_attempt_binder import bind_poc_attempts


def test_bind_poc_attempts_to_pre_submit_reasoning(tmp_path):
    trajectory = [
        {
            "id": 1,
            "source": "agent",
            "action": "record_reasoning",
            "args": {
                "kind": "source",
                "status": "confirmed",
                "file": "parser.c",
                "function": "parse",
                "line": 10,
                "var": "len",
                "code": "len = fgetc(fp);",
                "evidence": "code",
            },
        },
        {
            "id": 2,
            "source": "agent",
            "action": "record_reasoning",
            "args": {
                "kind": "edge",
                "status": "confirmed",
                "role": "tainted_read",
                "file": "parser.c",
                "function": "parse",
                "line": 10,
                "var": "len",
                "from": "attacker file",
                "to": "len",
                "relation": "parse",
                "code": "len = fgetc(fp);",
                "evidence": "code",
            },
        },
        {
            "id": 3,
            "source": "agent",
            "action": "run",
            "args": {"command": "bash submit.sh /workspace/poc.bin"},
        },
        {
            "id": 4,
            "source": "agent",
            "observation": "run",
            "cause": 3,
            "content": '{"task_id":"arvo:1","exit_code":1,"poc_id":"abc"}',
        },
        {
            "id": 5,
            "source": "agent",
            "action": "record_reasoning",
            "args": {
                "kind": "sink",
                "status": "confirmed",
                "file": "parser.c",
                "function": "parse",
                "line": 20,
                "var": "buf[i]",
                "code": "buf[i] = 0;",
                "evidence": "code",
            },
        },
    ]
    path = tmp_path / "trajectory"
    path.write_text(json.dumps(trajectory), encoding="utf-8")

    attempts = bind_poc_attempts(path)

    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["poc_path"] == "/workspace/poc.bin"
    assert attempt["submit_event_id"] == 3
    assert attempt["submit_observation_id"] == 4
    assert attempt["pre_submit_reasoning_event_ids"] == [1, 2]
    assert attempt["pre_submit_reasoning_state"]["primary_source"]["event_id"] == 1
    assert len(attempt["pre_submit_reasoning_state"]["trace"]) == 1
    assert attempt["target_exit_code"] == 1
    assert attempt["vul_exit_code"] == 1
    assert attempt["fix_exit_code"] is None
    assert attempt["cybergym_success"] is True


def test_bind_mcp_submit_candidate_attempt(tmp_path):
    mcp_result = {
        "submit_id": "submit_0001",
        "candidate_id": "cand_0001",
        "plan_id": "plan_0001",
        "success": False,
        "sanitizer_crash": False,
        "cybergym_response": {
            "task_id": "arvo:1",
            "exit_code": 0,
            "output": "Execution successful",
            "poc_id": "poc-1",
        },
        "command": {
            "command": "bash /workspace/submit.sh /workspace/candidates/cand_0001/input",
            "returncode": 0,
        },
    }
    trajectory = [
        {
            "id": 1,
            "source": "agent",
            "action": "record_reasoning",
            "args": {
                "kind": "source",
                "status": "confirmed",
                "file": "parser.c",
                "function": "parse",
                "line": 10,
                "var": "len",
                "code": "len = fgetc(fp);",
                "evidence": "code",
            },
        },
        {
            "id": 2,
            "source": "agent",
            "action": "call_tool_mcp",
            "args": {
                "name": "submit_candidate",
                "arguments": json.dumps(
                    {
                        "candidate_id": "cand_0001",
                        "submit_command": "bash /workspace/submit.sh {candidate_path}",
                        "workspace": "/workspace",
                    }
                ),
            },
        },
        {
            "id": 3,
            "source": "agent",
            "observation": "mcp",
            "cause": 2,
            "content": "MCP result:"
            + repr({"content": [{"type": "text", "text": json.dumps(mcp_result)}]}),
        },
    ]
    path = tmp_path / "trajectory"
    path.write_text(json.dumps(trajectory), encoding="utf-8")

    attempts = bind_poc_attempts(path)

    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["submit_transport"] == "mcp_submit_candidate"
    assert attempt["submit_id"] == "submit_0001"
    assert attempt["candidate_id"] == "cand_0001"
    assert attempt["plan_id"] == "plan_0001"
    assert attempt["poc_path"] == "/workspace/candidates/cand_0001/input"
    assert attempt["poc_id"] == "poc-1"
    assert attempt["target_exit_code"] == 0
    assert attempt["cybergym_success"] is False
    assert attempt["pre_submit_reasoning_event_ids"] == [1]


def test_bind_orphan_mcp_submit_candidate_observation(tmp_path):
    mcp_result = {
        "structuredContent": {
            "submit_id": "submit_0001",
            "candidate_id": "cand_0001",
            "plan_id": "adopted_plan_0001",
            "success": False,
            "sanitizer_crash": False,
            "cybergym_response": {
                "task_id": "arvo:1",
                "exit_code": 0,
                "output": "Reading 52 bytes from /tmp/poc\nExecution successful",
                "poc_id": "poc-1",
            },
            "command": {
                "command": "bash /workspace/submit.sh /workspace/candidates/cand_0001/input",
                "returncode": 0,
            },
        }
    }
    trajectory = [
        {
            "id": 1,
            "source": "agent",
            "action": "record_reasoning",
            "args": {
                "kind": "source",
                "status": "confirmed",
                "file": "parser.c",
                "function": "parse",
                "line": 10,
                "var": "len",
                "code": "len = fgetc(fp);",
                "evidence": "code",
            },
        },
        {
            "id": 2,
            "source": "agent",
            "observation": "mcp",
            "message": json.dumps(mcp_result),
        },
    ]
    path = tmp_path / "trajectory"
    path.write_text(json.dumps(trajectory), encoding="utf-8")

    attempts = bind_poc_attempts(path)

    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["submit_transport"] == "mcp_submit_candidate_observation"
    assert attempt["submit_id"] == "submit_0001"
    assert attempt["candidate_id"] == "cand_0001"
    assert attempt["poc_path"] == "/workspace/candidates/cand_0001/input"
    assert attempt["poc_id"] == "poc-1"
    assert attempt["pre_submit_reasoning_event_ids"] == [1]
