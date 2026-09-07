import json

from poc_generation import extract_context_visit as visit


def test_openhands_trajectory_extracts_command_and_output_context(tmp_path):
    sample = tmp_path / "ns" / "sample_1"
    checkpoint = sample / "checkpoint"
    checkpoint.mkdir(parents=True)
    events = [
        {
            "source": "user",
            "args": {"content": 'example "project/source/file.c"'},
        },
        {
            "source": "agent",
            "action": "run",
            "args": {
                "command": "sed -n '10,40p' repo-vul/src-vul/libyara/parser.c"
            },
        },
        {
            "source": "environment",
            "content": "12 int yr_parser_lookup_loop_variable(YR_COMPILER* compiler) {",
        },
    ]
    (checkpoint / "trajectory").write_text(json.dumps(events), encoding="utf-8")

    report = visit.build_context_visit(sample)

    assert report["schema_version"] == "gt-context-v1"
    assert any(
        {
            "file": "libyara/parser.c",
            "function": "yr_parser_lookup_loop_variable",
            "line": 12,
        }.items()
        <= item.items()
        for item in report["context"]
    )
    assert "project/source/file.c" not in json.dumps(report)


def test_claude_jsonl_extracts_read_file_and_function(tmp_path):
    sample = tmp_path / "ns" / "arvo_1"
    checkpoint = sample / "checkpoint"
    checkpoint.mkdir(parents=True)
    lines = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/workspace/repo-vul/src-vul/gnutls/lib/x509/pkcs12.c"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": "617 int gnutls_pkcs12_get_bag(gnutls_pkcs12_t pkcs12) {",
                    }
                ]
            },
        },
    ]
    (checkpoint / "claude_stdout.jsonl").write_text(
        "\n".join(json.dumps(item) for item in lines) + "\n",
        encoding="utf-8",
    )

    report = visit.build_context_visit(sample)

    assert any(
        item["file"] == "gnutls/lib/x509/pkcs12.c"
        and item["function"] == "gnutls_pkcs12_get_bag"
        and item["line"] == 617
        for item in report["context"]
    )


def test_trae_session_jsonl_extracts_exec_output_context(tmp_path):
    sample = tmp_path / "ns" / "arvo_2"
    session_dir = sample / "checkpoint" / "trae_home" / "cli" / "sessions" / "2026" / "08" / "23"
    session_dir.mkdir(parents=True)
    call_id = "call_1"
    lines = [
        {
            "type": "history_mutation",
            "payload": {
                "items": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": call_id,
                        "arguments": json.dumps(
                            {
                                "cmd": "sed -n '600,660p' repo-vul/src-vul/gnutls/lib/x509/pkcs12.c"
                            }
                        ),
                    }
                ]
            },
        },
        {
            "type": "history_mutation",
            "payload": {
                "items": [
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": (
                            "Output:\n"
                            "647 int gnutls_pkcs12_get_bag(gnutls_pkcs12_t pkcs12) {\n"
                        ),
                    }
                ]
            },
        },
    ]
    (session_dir / "rollout.jsonl").write_text(
        "\n".join(json.dumps(item) for item in lines) + "\n",
        encoding="utf-8",
    )

    report = visit.build_context_visit(sample)

    assert any(
        item["file"] == "gnutls/lib/x509/pkcs12.c"
        and item["function"] == "gnutls_pkcs12_get_bag"
        and item["line"] == 647
        for item in report["context"]
    )


def test_context_visit_manifest_update(tmp_path):
    sample = tmp_path / "ns" / "sample_1"
    checkpoint = sample / "checkpoint"
    checkpoint.mkdir(parents=True)
    (sample / "manifest.json").write_text(
        json.dumps({"sample_id": "sample_1", "harness": "codex", "model": "m"}),
        encoding="utf-8",
    )
    (checkpoint / "codex_stdout.txt").write_text(
        "codex\nexec\n/bin/bash -lc 'grep -n \"parse\" repo-vul/src-vul/src/a.c'\n"
        " succeeded:\nrepo-vul/src-vul/src/a.c:5:int parse(void) {\n",
        encoding="utf-8",
    )

    assert visit.write_context_visit(sample, overwrite=True, update_manifest_flag=True)

    manifest = json.loads((sample / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["context_visit"]["path"] == "context_visit.json"
    assert manifest["context_visit"]["context_count"] >= 1
