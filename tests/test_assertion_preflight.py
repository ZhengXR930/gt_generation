import json

from gt_generation.gt_toolkit.assertion_preflight import run_preflight
from gt_generation.gt_toolkit.assertions import assertion_content_hash


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_patch(path, added):
    path.write_text(
        "diff --git a/src/parser.c b/src/parser.c\n"
        "--- a/src/parser.c\n"
        "+++ b/src/parser.c\n"
        "@@ -1 +1,2 @@\n"
        " old\n"
        f"+{added}\n",
        encoding="utf-8",
    )


def _root_node():
    return {
        "invariant_id": "root.invariant",
        "role": "root_cause",
        "verified": True,
        "file": "src/parser.c",
        "function": "parse",
        "line": 10,
        "operands": ["index", "count"],
        "relation": {"op": "lt", "left": "index", "right": "count"},
    }


def test_preflight_rejects_harness_invariant_before_runtime(tmp_path):
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [
            {
                "id": "root.assertion",
                "kind": "required",
                "at": "guard",
                "mechanism": "bounds",
                "check": ["lt", "$guard.index", "$guard.count"],
                "invariants": ["root.invariant"],
            },
            {
                "id": "sink.assertion",
                "kind": "observed",
                "at": "sink",
                "check": ["eq", "$sink.left", "$sink.right"],
                "invariants": ["sink.invariant"],
            },
        ],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    invariants = {
        "root_cause_criterion": {"invariant_id": "root.invariant"},
        "nodes": [
            _root_node(),
            {
                "invariant_id": "sink.invariant",
                "role": "sink",
                "verified": True,
                "file": "tests/fuzz/parser_fuzzer.c",
                "function": "LLVMFuzzerTestOneInput",
                "line": 20,
                "operands": ["left", "right"],
                "relation": {"op": "eq", "left": "left", "right": "right"},
            }
        ],
        "edges": [],
    }
    _write(tmp_path / "spec.json", spec)
    _write(tmp_path / "invariants.json", invariants)
    _write(
        tmp_path / "fields.json",
        {
            "bindings": {
                "guard.index": "index",
                "guard.count": "count",
                "sink.left": "left",
                "sink.right": "right",
            }
        },
    )
    _write(
        tmp_path / "events.json",
        {
            "locations": {
                "guard": {"file": "src/parser.c", "function": "parse", "line": 10},
                "sink": {
                    "file": "tests/fuzz/parser_fuzzer.c",
                    "function": "LLVMFuzzerTestOneInput",
                    "line": 20,
                },
            }
        },
    )

    report = run_preflight(
        tmp_path / "spec.json",
        tmp_path / "invariants.json",
        tmp_path / "fields.json",
        tmp_path / "events.json",
    )

    assert report["ok"] is False
    assert any("unscored fuzzing harness" in error for error in report["errors"])


def test_preflight_rejects_pointer_owner_identity_as_lifetime_root(tmp_path):
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [
            {
                "id": "root.assertion",
                "kind": "required",
                "at": "root",
                "mechanism": "lifetime",
                "check": ["ne", "$root.ptr", "$root.owner"],
                "invariants": ["root.invariant"],
            }
        ],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    _write(tmp_path / "spec.json", spec)
    _write(
        tmp_path / "invariants.json",
        {
            "root_cause_criterion": {"invariant_id": "root.invariant"},
            "nodes": [
                {
                    "invariant_id": "root.invariant",
                    "role": "root_cause",
                    "verified": True,
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 10,
                    "operands": ["cursor_ptr", "owner_buffer"],
                    "relation": {
                        "op": "ne",
                        "left": "cursor_ptr",
                        "right": "owner_buffer",
                    },
                }
            ],
            "edges": [],
        },
    )
    _write(
        tmp_path / "fields.json",
        {
            "bindings": {
                "root.ptr": {"expr": "cursor_ptr"},
                "root.owner": {"expr": "owner_buffer"},
            }
        },
    )
    _write(
        tmp_path / "events.json",
        {
            "locations": {
                "root": {
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 10,
                }
            }
        },
    )

    report = run_preflight(
        tmp_path / "spec.json",
        tmp_path / "invariants.json",
        tmp_path / "fields.json",
        tmp_path / "events.json",
    )

    assert report["ok"] is False
    assert any("pointer vs owner/base/buffer equality" in error for error in report["errors"])


def test_preflight_rejects_sentinel_check_before_assignment(tmp_path):
    source_root = tmp_path / "srcroot"
    source_file = source_root / "src" / "parser.c"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        "int parse(struct State *state) {\n"
        "  if (state->ready) {\n"
        "    use(state->buffer);\n"
        "    state->buffer = NULL;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [
            {
                "id": "root.assertion",
                "kind": "required",
                "at": "root",
                "mechanism": "lifetime",
                "check": ["eq", "$root.buffer", "$root.null"],
                "invariants": ["root.invariant"],
            }
        ],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    _write(tmp_path / "spec.json", spec)
    _write(
        tmp_path / "invariants.json",
        {
            "root_cause_criterion": {"invariant_id": "root.invariant"},
            "nodes": [
                {
                    "invariant_id": "root.invariant",
                    "role": "root_cause",
                    "verified": True,
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 3,
                    "operands": ["state->buffer", "NULL"],
                    "relation": {
                        "op": "eq",
                        "left": "state->buffer",
                        "right": "NULL",
                    },
                }
            ],
            "edges": [],
        },
    )
    _write(
        tmp_path / "fields.json",
        {
            "bindings": {
                "root.buffer": {"expr": "state->buffer"},
                "root.null": {"expr": "NULL"},
            }
        },
    )
    _write(
        tmp_path / "events.json",
        {
            "locations": {
                "root": {
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 3,
                }
            }
        },
    )

    report = run_preflight(
        tmp_path / "spec.json",
        tmp_path / "invariants.json",
        tmp_path / "fields.json",
        tmp_path / "events.json",
        source_root=source_root,
    )

    assert report["ok"] is False
    assert any("before the source assignment" in error for error in report["errors"])


def test_preflight_rejects_cleanup_sentinel_as_lifetime_root(tmp_path):
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [
            {
                "id": "root.assertion",
                "kind": "required",
                "at": "root",
                "mechanism": "lifetime",
                "check": ["eq", "$root.buffer", "$root.null"],
                "invariants": ["root.invariant"],
            }
        ],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    _write(tmp_path / "spec.json", spec)
    _write(
        tmp_path / "invariants.json",
        {
            "root_cause_criterion": {"invariant_id": "root.invariant"},
            "nodes": [
                {
                    "invariant_id": "root.invariant",
                    "role": "root_cause",
                    "verified": True,
                    "file": "src/parser.c",
                    "function": "free_single_stream",
                    "line": 10,
                    "operands": ["state->buffer", "NULL"],
                    "relation": {
                        "op": "eq",
                        "left": "state->buffer",
                        "right": "NULL",
                    },
                }
            ],
            "edges": [],
        },
    )
    _write(
        tmp_path / "fields.json",
        {
            "bindings": {
                "root.buffer": {"expr": "state->buffer"},
                "root.null": {"expr": "NULL"},
            }
        },
    )
    _write(
        tmp_path / "events.json",
        {
            "locations": {
                "root": {
                    "file": "src/parser.c",
                    "function": "free_single_stream",
                    "line": 10,
                }
            }
        },
    )

    report = run_preflight(
        tmp_path / "spec.json",
        tmp_path / "invariants.json",
        tmp_path / "fields.json",
        tmp_path / "events.json",
    )

    assert report["ok"] is False
    assert any("cleanup-time sentinel state" in error for error in report["errors"])


def test_preflight_rejects_prose_binding_used_by_assertion(tmp_path):
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [
            {
                "id": "root.assertion",
                "kind": "required",
                "at": "root",
                "mechanism": "lifetime",
                "check": ["eq", "$root.free_before_use", "$root.false_literal"],
                "invariants": ["root.invariant"],
            }
        ],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    _write(tmp_path / "spec.json", spec)
    _write(
        tmp_path / "invariants.json",
        {
            "root_cause_criterion": {"invariant_id": "root.invariant"},
            "nodes": [
                {
                    "invariant_id": "root.invariant",
                    "role": "root_cause",
                    "verified": True,
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 10,
                    "operands": ["free_before_use", "false"],
                    "relation": {
                        "op": "eq",
                        "left": "free_before_use",
                        "right": "false",
                    },
                }
            ],
            "edges": [],
        },
    )
    _write(
        tmp_path / "fields.json",
        {
            "bindings": {
                "root.free_before_use": {
                    "expr": (
                        "free_stream executes before the later read recorded by "
                        "ASan for this witness"
                    )
                }
            }
        },
    )
    _write(
        tmp_path / "events.json",
        {
            "locations": {
                "root": {
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 10,
                }
            }
        },
    )

    report = run_preflight(
        tmp_path / "spec.json",
        tmp_path / "invariants.json",
        tmp_path / "fields.json",
        tmp_path / "events.json",
    )

    assert report["ok"] is False
    assert any("is prose" in error for error in report["errors"])


def test_preflight_commits_both_instrumentation_patches(tmp_path):
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [
            {
                "id": "root.assertion",
                "kind": "required",
                "at": "guard",
                "mechanism": "bounds",
                "check": ["lt", "$guard.index", "$guard.count"],
                "invariants": ["root.invariant"],
            }
        ],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    invariants = {
        "root_cause_criterion": {"invariant_id": "root.invariant"},
        "nodes": [_root_node()],
        "edges": [],
    }
    _write(tmp_path / "spec.json", spec)
    _write(tmp_path / "invariants.json", invariants)
    _write(
        tmp_path / "fields.json",
        {"bindings": {"guard.index": "index", "guard.count": "count"}},
    )
    _write(
        tmp_path / "events.json",
        {
            "locations": {
                "guard": {
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 10,
                }
            }
        },
    )
    vulnerable_patch = tmp_path / "vulnerable-instrumentation.patch"
    fixed_patch = tmp_path / "fixed-instrumentation.patch"
    _write_patch(vulnerable_patch, "vulnerable instrumentation")
    _write_patch(fixed_patch, "fixed instrumentation")

    report = run_preflight(
        tmp_path / "spec.json",
        tmp_path / "invariants.json",
        tmp_path / "fields.json",
        tmp_path / "events.json",
        vulnerable_patch,
        fixed_patch,
    )

    assert report["ok"] is True
    assert set(report["input_hashes"]) == {
        "spec.json",
        "invariants.json",
        "fields.json",
        "events.json",
        "vulnerable-instrumentation.patch",
        "fixed-instrumentation.patch",
    }
    assert report["input_hashes"]["vulnerable-instrumentation.patch"].startswith(
        "sha256:"
    )


def test_preflight_rejects_malformed_instrumentation_patch(tmp_path):
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [
            {
                "id": "root.assertion",
                "kind": "required",
                "at": "guard",
                "mechanism": "bounds",
                "check": ["lt", "$guard.index", "$guard.count"],
                "invariants": ["root.invariant"],
            }
        ],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    _write(tmp_path / "spec.json", spec)
    _write(
        tmp_path / "invariants.json",
        {
            "root_cause_criterion": {"invariant_id": "root.invariant"},
            "nodes": [_root_node()],
            "edges": [],
        },
    )
    _write(
        tmp_path / "fields.json",
        {"bindings": {"guard.index": "index", "guard.count": "count"}},
    )
    _write(
        tmp_path / "events.json",
        {
            "locations": {
                "guard": {
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 10,
                }
            }
        },
    )
    vulnerable_patch = tmp_path / "vulnerable-instrumentation.patch"
    fixed_patch = tmp_path / "fixed-instrumentation.patch"
    vulnerable_patch.write_text(
        "diff --git a/src/parser.c b/src/parser.c\n"
        "--- a/src/parser.c\n"
        "+++ b/src/parser.c\n"
        "@@ -1,1 +1,99 @@\n"
        " old\n"
        "+broken\n",
        encoding="utf-8",
    )
    _write_patch(fixed_patch, "fixed instrumentation")

    report = run_preflight(
        tmp_path / "spec.json",
        tmp_path / "invariants.json",
        tmp_path / "fields.json",
        tmp_path / "events.json",
        vulnerable_patch,
        fixed_patch,
    )

    assert report["ok"] is False
    assert any(
        "invalid instrumentation patch vulnerable-instrumentation.patch"
        in error
        and "corrupt patch" in error
        for error in report["errors"]
    )
