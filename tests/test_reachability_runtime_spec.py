import json

from reachability.runtime_spec import (
    compile_runtime_spec,
    remap_checkpoints_to_workspace,
)


def _base_sample(tmp_path, command):
    sample = tmp_path / "secbench_case"
    target = sample / "_work" / "src" / "bin" / "target"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x7fELF")
    target.chmod(0o755)
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "reproduction_report.json").write_text(
        json.dumps({"command": command})
    )
    return sample


def test_runtime_spec_extracts_only_final_poc_invocation(tmp_path):
    sample = _base_sample(
        tmp_path,
        "/tmp/sample/build.sh 'set -e; make all; "
        "ASAN_OPTIONS=detect_leaks=0 ./bin/target --flag /gt/poc 2>&1 | tee /gt/log'",
    )
    spec = compile_runtime_spec(sample)

    assert spec.executable == "./bin/target"
    assert spec.arguments == ["--flag", "{poc}"]
    assert spec.environment == {"ASAN_OPTIONS": "detect_leaks=0"}


def test_runtime_spec_unwraps_libtool_script(tmp_path):
    sample = _base_sample(tmp_path, "./bin/target /gt/poc")
    wrapper = sample / "_work" / "src" / "bin" / "target"
    wrapper.write_text(
        "#!/bin/sh\n"
        'LD_LIBRARY_PATH="/gt/_work/src/.libs:$LD_LIBRARY_PATH"\n'
    )
    actual = wrapper.parent / ".libs" / "target"
    actual.parent.mkdir()
    actual.write_bytes(b"\x7fELF")
    actual.chmod(0o755)

    spec = compile_runtime_spec(sample)

    assert spec.executable == "./bin/.libs/target"
    assert spec.environment["LD_LIBRARY_PATH"] == "/gt/_work/src/.libs"


def test_runtime_checkpoint_line_is_remapped_by_frozen_statement(tmp_path):
    sample = tmp_path / "secbench_case"
    source = sample / "_work" / "src" / "src" / "parser.c"
    source.parent.mkdir(parents=True)
    source.write_text("inserted();\ninserted();\nconsume(input);\n")
    checkpoints = [{
        "kind": "source",
        "file": "src/parser.c",
        "function": "parse",
        "line": 1,
        "code": "consume(input);",
    }]

    result = remap_checkpoints_to_workspace(checkpoints, sample)

    assert result[0]["gt_line"] == 1
    assert result[0]["line"] == 3
