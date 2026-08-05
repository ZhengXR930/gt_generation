import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "external" / "cybergym" / "src"))

from poc_generation.poc_generator.run_local_sample import (
    minimize_submission_command,
    render_readme,
    runtime_triggered,
    unwrap_nested_docker_command,
)
from poc_generation.poc_generator.rerun_model_batches import local_result_is_complete


def test_readme_exposes_issue_but_not_saved_crash_evidence():
    text = render_readme(
        "sample",
        {"project": "demo", "public_id": "CVE-X", "issue_description": "public issue"},
    )
    assert "public issue" in text
    assert "Saved crash summary" not in text
    assert "ground_truth" not in text


def test_nested_docker_and_build_prefix_are_removed_from_submission_command():
    command = (
        "docker run --rm image bash -lc "
        "'make -j2 && ASAN_OPTIONS=abort_on_error=1 ./target /gt/poc'"
    )
    inner = unwrap_nested_docker_command(command)
    assert minimize_submission_command(inner) == (
        "ASAN_OPTIONS=abort_on_error=1 ./target /gt/poc"
    )


def test_submission_command_keeps_required_working_directory():
    command = "cd /gt/_work/build && ./bin/target /gt/poc"
    assert minimize_submission_command(command) == command


def test_trigger_requires_runtime_detector_evidence():
    assert runtime_triggered("ERROR: AddressSanitizer: heap-buffer-overflow", 1)
    assert not runtime_triggered("ordinary parser rejection", 1)


def test_local_complete_requires_checkpoint_trace_and_submission_artifacts(tmp_path):
    result = tmp_path / "sample"
    attempt = result / "submissions" / "a1"
    attempt.mkdir(parents=True)
    (result / "checkpoint").mkdir()
    (result / "fine_trace.json").write_text("[]")
    for name in (
        "poc.bin", "candidate_trace.json", "candidate_trace.response.txt",
        "result.json", "runtime_output.txt",
    ):
        (attempt / name).write_text("")
    (result / "manifest.json").write_text(json.dumps({
        "evaluation_protocol": "poc_trace_per_submission_v2_local",
        "max_iter": 100,
        "status": "iteration_cap",
        "submission_attempts": [{"attempt_id": "a1"}],
    }))
    assert local_result_is_complete(result)
