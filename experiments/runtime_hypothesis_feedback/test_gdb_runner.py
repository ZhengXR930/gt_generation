from experiments.runtime_hypothesis_feedback.gdb_runner import (
    runtime_checked,
    target_arguments,
)


def test_afl_target_does_not_receive_libfuzzer_flag(tmp_path):
    executable = tmp_path / "afl_target"
    executable.write_bytes(b"prefix This binary is built for AFL-fuzz suffix")
    poc = tmp_path / "poc"
    assert target_arguments(executable, poc) == [str(executable), str(poc)]


def test_libfuzzer_target_receives_runs_flag(tmp_path):
    executable = tmp_path / "libfuzzer_target"
    executable.write_bytes(b"ordinary target")
    poc = tmp_path / "poc"
    assert target_arguments(executable, poc) == [
        str(executable),
        "-runs=0",
        str(poc),
    ]


def test_target_startup_failure_is_not_a_completed_runtime_check():
    assert runtime_checked(0, [{"run_error": "startup exited with code 127"}]) is False
    assert runtime_checked(0, []) is True
    assert runtime_checked(1, []) is False
