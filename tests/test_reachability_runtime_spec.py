import json
import tarfile

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


def test_runtime_spec_hydrates_from_runtime_archive(tmp_path):
    sample = tmp_path / "secbench_case"
    sample.mkdir()
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "sample_info.json").write_text(json.dumps({
        "sample_id": "secbench_case",
        "repo": "https://example.test/project.git",
        "vulnerable_commit": "deadbeef",
    }))
    (sample / "runtime_spec.json").write_text(json.dumps({
        "sample_id": "secbench_case",
        "backend": "local_workspace",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "./bin/target",
        "arguments": ["{poc}"],
        "environment": {},
        "input_placeholder": "{poc}",
        "source": "runtime_spec.json",
    }))
    staged = tmp_path / "staged" / "_work" / "src" / "bin"
    staged.mkdir(parents=True)
    target = staged / "target"
    target.write_bytes(b"\x7fELF")
    target.chmod(0o755)
    with tarfile.open(sample / "runtime_work.tar.gz", "w:gz") as tar:
        tar.add(tmp_path / "staged" / "_work", arcname="_work")

    spec = compile_runtime_spec(sample)

    assert spec.executable == "./bin/target"
    assert (sample / "_work" / "src" / "bin" / "target").is_file()


def test_runtime_spec_unwraps_env_executable(tmp_path):
    sample = tmp_path / "secbench_case"
    target = sample / "_work" / "src" / "build" / "cjpeg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x7fELF")
    target.chmod(0o755)
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "ground_truth.json").write_text(json.dumps({
        "poc": {
            "trigger": (
                "./build.sh 'env ASAN_OPTIONS=detect_leaks=0 "
                "LD_LIBRARY_PATH=/gt/_work/src/build "
                "/gt/_work/src/build/cjpeg -outfile /tmp/out.jpg /gt/poc'"
            ),
        },
    }))

    spec = compile_runtime_spec(sample)

    assert spec.executable == "/gt/_work/src/build/cjpeg"
    assert spec.arguments == ["-outfile", "/tmp/out.jpg", "{poc}"]
    assert spec.environment == {
        "ASAN_OPTIONS": "detect_leaks=0",
        "LD_LIBRARY_PATH": "/gt/_work/src/build",
    }


def test_runtime_spec_relocates_unique_executable_with_same_basename(tmp_path):
    sample = tmp_path / "secbench_case"
    target = sample / "_work" / "bin" / "cjpeg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x7fELF")
    target.chmod(0o755)
    (sample / "_work" / "src").mkdir()
    (sample / "_work" / "src" / "parser.c").write_text("int main(void) { return 0; }\n")
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "runtime_spec.json").write_text(json.dumps({
        "sample_id": "secbench_case",
        "backend": "local_workspace",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "/gt/_work/src/build/cjpeg",
        "arguments": ["{poc}"],
        "environment": {},
        "input_placeholder": "{poc}",
        "source": "runtime_spec.json",
    }))

    spec = compile_runtime_spec(sample)

    assert spec.executable == "/gt/_work/bin/cjpeg"
    assert spec.source.endswith("+basename_relocated")


def test_runtime_spec_does_not_guess_between_duplicate_basenames(tmp_path):
    sample = tmp_path / "secbench_case"
    (sample / "_work" / "src").mkdir(parents=True)
    (sample / "_work" / "src" / "parser.c").write_text("int main(void) { return 0; }\n")
    for parent in (sample / "_work" / "bin1", sample / "_out"):
        parent.mkdir(parents=True)
        target = parent / "target"
        target.write_bytes(b"\x7fELF")
        target.chmod(0o755)
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "runtime_spec.json").write_text(json.dumps({
        "sample_id": "secbench_case",
        "backend": "local_workspace",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "./missing/target",
        "arguments": ["{poc}"],
        "environment": {},
        "input_placeholder": "{poc}",
        "source": "runtime_spec.json",
    }))

    import pytest
    from reachability.runtime_spec import RuntimeSpecError

    with pytest.raises(RuntimeSpecError, match="runtime executable is missing"):
        compile_runtime_spec(sample)


def test_runtime_spec_prefers_unique_workdir_match(tmp_path):
    sample = tmp_path / "secbench_case"
    (sample / "_work" / "src").mkdir(parents=True)
    for target in (
        sample / "_work" / "src" / "cjpeg",
        sample / "_work" / "bin" / "cjpeg",
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x7fELF")
        target.chmod(0o755)
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "runtime_spec.json").write_text(json.dumps({
        "sample_id": "secbench_case", "backend": "local_workspace",
        "image": "gt-memory-env:latest", "workdir": "/gt/_work/src",
        "executable": "/gt/_work/src/build/cjpeg",
        "arguments": ["{poc}"], "environment": {},
        "input_placeholder": "{poc}", "source": "runtime_spec.json",
    }))

    spec = compile_runtime_spec(sample)

    assert spec.executable == "/gt/_work/src/cjpeg"


def test_runtime_spec_uses_recorded_oss_fuzz_target(tmp_path):
    sample = tmp_path / "osv_case"
    (sample / "_work" / "src").mkdir(parents=True)
    (sample / "_work" / "src" / "parser.c").write_text("int main(void) { return 0; }\n")
    target = sample / "_out" / "fuzz"
    target.parent.mkdir()
    target.write_bytes(b"\x7fELF")
    target.chmod(0o755)
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "sample_info.json").write_text(json.dumps({
        "sample_id": "osv_case", "oss_fuzz_target": "fuzz"
    }))
    (sample / "runtime_spec.json").write_text(json.dumps({
        "sample_id": "osv_case", "backend": "local_workspace",
        "image": "gt-memory-env:latest", "workdir": "/gt/_work/src",
        "executable": "./test/fuzzer", "arguments": ["{poc}"],
        "environment": {}, "input_placeholder": "{poc}",
        "source": "runtime_spec.json",
    }))

    spec = compile_runtime_spec(sample)

    assert spec.executable == "/gt/_out/fuzz"


def test_runtime_spec_rebuilds_artifacts_from_runtime_build_recipe(tmp_path, monkeypatch):
    sample = tmp_path / "secbench_case"
    (sample / "_work" / "src").mkdir(parents=True)
    (sample / "_work" / "src" / "parser.c").write_text("int main(void) { return 0; }\n")
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "runtime_spec.json").write_text(json.dumps({
        "sample_id": "secbench_case",
        "backend": "local_workspace",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "./bin/target",
        "arguments": ["{poc}"],
        "environment": {},
        "input_placeholder": "{poc}",
        "source": "runtime_spec.json",
    }))
    (sample / "runtime_build.json").write_text(json.dumps({
        "schema_version": "gt-runtime-build-v1",
        "sample_id": "secbench_case",
        "commands": [{
            "source": "test",
            "command": "make -j8",
            "run_as_root": False,
        }],
    }))

    def fake_build(gt_dir):
        assert gt_dir == sample.resolve()
        target = sample / "_work" / "src" / "bin" / "target"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x7fELF")
        target.chmod(0o755)
        return {"prepared": True, "built": True}

    monkeypatch.setattr("reachability.runtime_spec.build_runtime_workspace", fake_build)

    spec = compile_runtime_spec(sample)

    assert spec.executable == "./bin/target"


def test_runtime_spec_parses_multiline_build_wrapper_command(tmp_path):
    sample = tmp_path / "secbench_case"
    sample.mkdir()
    (sample / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample / "reproduction_report.json").write_text(json.dumps({
        "command": "/gt/build.sh 'set -euo pipefail\ncd /gt/_work/src\n./build/janet /gt/poc'",
    }))

    spec = compile_runtime_spec(sample, require_artifacts=False, prefer_frozen=False)

    assert spec.workdir == "/gt/_work/src"
    assert spec.executable == "./build/janet"
    assert spec.arguments == ["{poc}"]


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
