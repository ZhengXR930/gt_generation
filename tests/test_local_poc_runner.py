import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "external" / "cybergym" / "src"))

from poc_generation.poc_generator.run_local_sample import (
    LocalExecutionBridge,
    check_runtime_readiness,
    load_runtime_spec,
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


def test_runtime_spec_reads_only_normalized_private_trigger(tmp_path):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {"trigger": "./target -runs=1 /gt/poc"},
    }))
    (tmp_path / "reachability_report.json").write_text(json.dumps({
        "sanitizer_observed": "address",
        "debug_command": {"command": ["must", "not", "be", "used"]},
    }))

    command, metadata = load_runtime_spec(tmp_path)

    assert command == "./target -runs=1 /gt/poc"
    assert metadata == {
        "detector": "address",
        "source": "normalized_private_gt_trigger",
    }


def test_runtime_spec_rejects_free_text_instead_of_inferring_command(tmp_path):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {
            "trigger": "Run `./target -runs=1 /gt/poc` on the saved input.",
        },
    }))
    (tmp_path / "reachability_report.json").write_text(json.dumps({
        "debug_command": {
            "command": ["gdb", "--args", "./fallback", "/gt/poc"],
        },
    }))

    with pytest.raises(RuntimeError, match="non-executable poc.trigger"):
        load_runtime_spec(tmp_path)


def test_runtime_readiness_accepts_cloneable_source(tmp_path, monkeypatch):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {"trigger": "./target /gt/poc"},
    }))
    (tmp_path / "sample_info.json").write_text(json.dumps({
        "repo": "https://example.test/project.git",
        "vulnerable_commit": "deadbeef",
    }))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""),
    )

    readiness = check_runtime_readiness(tmp_path)

    assert readiness["ready"] is True
    assert readiness["source_strategy"] == "clone_commit"
    assert readiness["required_images"] == ["gt-memory-env:latest", "alpine:3.23"]


def test_runtime_readiness_rejects_missing_runtime_images(tmp_path, monkeypatch):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {"trigger": "./target /gt/poc"},
    }))
    (tmp_path / "sample_info.json").write_text(json.dumps({
        "repo": "https://example.test/project.git",
        "vulnerable_commit": "deadbeef",
    }))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="No such image",
        ),
    )

    with pytest.raises(RuntimeError, match="runtime images are missing"):
        check_runtime_readiness(tmp_path)


def test_local_bridge_uses_active_docker_gateway(tmp_path, monkeypatch):
    bridge = LocalExecutionBridge.__new__(LocalExecutionBridge)
    bridge.server = SimpleNamespace(server_port=4321)
    docker_bridge = SimpleNamespace(
        attrs={"IPAM": {"Config": [{"Gateway": "172.20.0.1"}]}}
    )
    client = SimpleNamespace(
        networks=SimpleNamespace(get=lambda name: docker_bridge)
    )
    monkeypatch.delenv("OPENHANDS_EVAL_HOST_GATEWAY", raising=False)
    from poc_generation.poc_generator import run_sample

    monkeypatch.setattr(run_sample.docker, "from_env", lambda: client)

    assert bridge.url == "http://172.20.0.1:4321"


def test_local_bridge_decodes_timeout_bytes(tmp_path, monkeypatch):
    bridge = LocalExecutionBridge.__new__(LocalExecutionBridge)
    bridge.workspace = tmp_path
    bridge._execution_lock = threading.Lock()
    monkeypatch.setattr(bridge, "_transport_admin", lambda command: None)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs["timeout"],
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr(subprocess, "run", timeout)

    result = bridge.run_command("./target /gt/poc", 60)

    assert result["exit_code"] == 124
    assert "partial stdoutpartial stderr" in result["output"]
    assert result["output"].endswith("execution timed out\n")


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
