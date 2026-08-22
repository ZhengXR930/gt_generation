import json

from gt_generation.gt_toolkit import context_trace


def test_build_context_checkpoints_uses_gt_trace_and_invariants():
    gt = {
        "sample_id": "sample",
        "source": {
            "file": "/gt/_work/src/src/parser.c",
            "function": "parse",
            "line": 10,
            "code": "input = data;",
        },
        "root_cause": {
            "file": "src/parser.c",
            "function": "parse",
            "line": 20,
        },
        "sink": {
            "file": "src/parser.c",
            "function": "parse",
            "line": 30,
        },
        "reachability_checkpoints": {
            "parser_admitted": {
                "file": "src/api.c",
                "function": "accept_input",
                "line": 5,
            }
        },
        "fine_trace": [
            {
                "step": 1,
                "file": "/gt/_work/src/src/parser.c",
                "function": "parse",
                "line": 10,
            },
            {
                "step": 2,
                "file": "src/parser.c",
                "function": "helper",
                "line": 15,
            },
        ],
    }
    invariants = {
        "nodes": [
            {
                "invariant_id": "N1",
                "role": "root_cause",
                "file": "src/parser.c",
                "function": "parse",
                "line": 20,
            }
        ],
        "edges": [
            {
                "invariant_id": "E1",
                "type": "data",
                "from_file": "src/parser.c",
                "from_function": "parse",
                "from_line": 10,
                "to_file": "src/parser.c",
                "to_function": "parse",
                "to_line": 30,
            }
        ],
    }

    checkpoints = context_trace.build_context_checkpoints(gt, invariants)
    by_kind = {item["kind"]: item for item in checkpoints}

    assert by_kind["parser_admitted"]["function"] == "accept_input"
    assert by_kind["source"]["file"] == "src/parser.c"
    assert by_kind["fine_trace"]["fine_trace_step"] == 2
    assert by_kind["invariant_node:root_cause"]["invariant_id"] == "N1"
    assert by_kind["invariant_edge_to"]["edge_type"] == "data"
    assert {
        item["kind"]
        for item in checkpoints
        if item["function"] == "parse" and item["line"] == 10
    } == {"source", "fine_trace", "invariant_edge_from"}


def test_context_from_events_records_project_frames_in_execution_order(tmp_path):
    src = tmp_path / "repo"
    (src / "src").mkdir(parents=True)
    (src / "src" / "parser.c").write_text(
        "int main(void) { return parse(); }\n"
        "int parse(void) { return sink(); }\n"
        "int sink(void) { return 0; }\n",
        encoding="utf-8",
    )
    events = [
        {
            "stack": [
                {"file": "/usr/lib/libc.so", "function": "__libc_start_main", "line": 1},
                {"file": "/build/_work/src/src/parser.c", "function": "sink", "line": 3},
                {"file": "/build/_work/src/src/parser.c", "function": "parse", "line": 2},
                {"file": "/build/_work/src/src/parser.c", "function": "main", "line": 1},
            ]
        }
    ]

    context = context_trace._context_from_events(events, codebase=src)

    assert [item["function"] for item in context] == ["main", "parse", "sink"]
    assert context[0]["code"] == "int main(void) { return parse(); }"


def test_context_trace_errors_reject_empty_or_malformed_context(tmp_path):
    valid = {
        "schema_version": "gt-context-trace-v1",
        "sample_id": tmp_path.name,
        "collection": {},
        "context": [{"file": "src/parser.c", "function": "parse", "line": 12}],
    }
    assert context_trace.context_trace_errors(tmp_path, valid) == []

    invalid = json.loads(json.dumps(valid))
    invalid["context"] = [{"file": "src/parser.c", "function": "", "line": 0}]

    errors = context_trace.context_trace_errors(tmp_path, invalid)
    assert "context_trace.json context[0] missing function" in errors
    assert "context_trace.json context[0] missing positive line" in errors


def test_split_command_env_keeps_gdb_argv_executable_first():
    env, argv = context_trace._split_command_env(
        "ASAN_OPTIONS=detect_leaks=0 UBSAN_OPTIONS=print_stacktrace=1 ./target /gt/poc"
    )

    assert env == {
        "ASAN_OPTIONS": "detect_leaks=0",
        "UBSAN_OPTIONS": "print_stacktrace=1",
    }
    assert argv == ["./target", "/gt/poc"]
