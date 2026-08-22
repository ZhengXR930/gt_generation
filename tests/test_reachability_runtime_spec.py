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


def test_runtime_spec_uses_reachability_debug_command_fallback(tmp_path):
    sample = tmp_path / "nvd_case"
    sample.mkdir()
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "ground_truth.json").write_text(json.dumps({
        "poc": {"trigger": "Run the saved witness input through the reproducer."},
    }))
    (sample / "reachability_report.json").write_text(json.dumps({
        "debug_command": {
            "command": [
                "gdb", "--batch", "-q", "-x", "/repo/gdb.py",
                "--args", "/tmp/repro", "/gt/poc",
            ],
        },
    }))

    spec = compile_runtime_spec(sample, require_artifacts=False)

    assert spec.source == "reachability_report.debug_command"
    assert spec.executable == "/tmp/repro"
    assert spec.arguments == ["{poc}"]


def test_runtime_spec_preserves_quoted_one_liner_semicolon(tmp_path):
    sample = tmp_path / "ruby_case"
    sample.mkdir()
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "ground_truth.json").write_text(json.dumps({
        "poc": {
            "trigger": (
                "./build.sh 'ruby -Ilib -r ox -e "
                "'\"'\"'data = File.binread(\"/gt/poc\"); Ox.parse(data)'\"'\"''"
            ),
        },
    }))

    spec = compile_runtime_spec(sample, require_artifacts=False)

    assert spec.executable == "ruby"
    assert spec.arguments == [
        "-Ilib",
        "-r",
        "ox",
        "-e",
        'data = File.binread("{poc}"); Ox.parse(data)',
    ]


def test_runtime_spec_normalizes_gt_workdir_executable(tmp_path):
    sample = tmp_path / "gpac_case"
    sample.mkdir()
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "ground_truth.json").write_text(json.dumps({
        "poc": {
            "trigger": "./build.sh 'cd /gt && ./_work/src/bin/gcc/MP4Box -dash 1000 /gt/poc'",
        },
    }))

    spec = compile_runtime_spec(sample, require_artifacts=False)

    assert spec.workdir == "/gt/_work/src"
    assert spec.executable == "./bin/gcc/MP4Box"
    assert spec.arguments == ["-dash", "1000", "{poc}"]


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
