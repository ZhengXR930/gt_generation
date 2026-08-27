import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "external" / "cybergym" / "src"))

from harness_runtime.openhands.local import (
    LocalExecutionBridge,
    check_runtime_readiness,
    command_from_runtime_spec,
    copy_source,
    _extract_analysis_from_text,
    load_json,
    load_runtime_spec,
    minimize_submission_command,
    materialize_agent_visible_source_tree,
    render_description,
    write_config,
    normalize_runtime_build_command,
    runtime_triggered,
    scrub_agent_visible_runtime_build_artifacts,
    unwrap_nested_docker_command,
)
from poc_generation.run_harness import result_is_complete
from evaluator.reachability.runtime_spec import RuntimeSpec


def test_description_exposes_issue_but_not_saved_crash_evidence():
    text = render_description(
        "sample",
        {"project": "demo", "public_id": "CVE-X", "issue_description": "public issue"},
    )
    assert "public issue" in text
    assert "Saved crash summary" not in text
    assert "ground_truth" not in text


def test_workspace_description_is_loaded_from_curated_file(tmp_path):
    from harness_runtime.openhands.local import write_workspace_description

    sample_dir = tmp_path / "sample"
    workspace = tmp_path / "workspace"
    sample_dir.mkdir()
    workspace.mkdir()
    (sample_dir / "description.txt").write_text("curated public description\n", encoding="utf-8")

    write_workspace_description(
        workspace,
        sample_dir,
        "sample",
        {"project": "demo", "public_id": "CVE-X", "issue_description": "fallback"},
    )

    assert (workspace / "description.txt").read_text(encoding="utf-8") == "curated public description\n"


def test_openhands_local_config_can_disable_runtime_dns(tmp_path, monkeypatch):
    if tomllib is None:
        pytest.skip("tomllib unavailable")
    monkeypatch.setenv("OPENHANDS_RUNTIME_DISABLE_DNS", "1")
    config_path = tmp_path / "config.toml"

    write_config(config_path, workspace=tmp_path / "workspace", log_dir=tmp_path / "run", model="glm-5.2", base_url="https://example.test/openai", api_version="2024-03-01-preview", native_tool_calling=True)

    runtime_kwargs = tomllib.loads(config_path.read_text(encoding="utf-8"))["sandbox"]["docker_runtime_kwargs"]
    assert runtime_kwargs["dns"] == ["127.0.0.1"]
    assert runtime_kwargs["dns_search"] == []


def test_runtime_spec_reads_only_normalized_private_trigger(tmp_path):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {"trigger": "./build.sh './target -runs=1 /gt/poc'"},
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
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "",
        "build_commands": [],
        "build_workdir": "/gt/_work/src",
    }


def test_runtime_spec_reads_frozen_runtime_spec_first(tmp_path):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {"trigger": "Natural-language trigger."},
    }))
    (tmp_path / "runtime_spec.json").write_text(json.dumps({
        "backend": "local_workspace",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "./build/sanitize/mutool",
        "arguments": ["draw", "-o", "/tmp/out-%d.png", "{poc}"],
        "environment": {"ASAN_OPTIONS": "detect_leaks=0"},
    }))
    (tmp_path / "reachability_report.json").write_text(json.dumps({
        "sanitizer_observed": "address",
        "debug_command": {
            "command": ["gdb", "--args", "./fallback", "/gt/poc"],
        },
    }))

    command, metadata = load_runtime_spec(tmp_path)

    assert command == (
        "ASAN_OPTIONS=detect_leaks=0 ./build/sanitize/mutool "
        "draw -o /tmp/out-%d.png /gt/poc"
    )
    assert metadata == {
        "detector": "address",
        "source": "runtime_spec.json",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "./build/sanitize/mutool",
        "build_commands": [],
        "build_workdir": "/gt/_work/src",
    }


def test_runtime_spec_falls_back_to_reachability_debug_command(tmp_path):
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

    command, metadata = load_runtime_spec(tmp_path)

    assert command == "./fallback /gt/poc"
    assert metadata == {
        "detector": "",
        "source": "reachability_report_debug_command",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "",
        "build_commands": [],
        "build_workdir": "/gt/_work/src",
    }


def test_runtime_spec_accepts_direct_private_trigger_with_poc_placeholder(tmp_path):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {
            "trigger": "ASAN_OPTIONS=abort_on_error=1 ./fuzzer {poc}",
        },
    }))

    command, metadata = load_runtime_spec(tmp_path)

    assert command == "ASAN_OPTIONS=abort_on_error=1 ./fuzzer /gt/poc"
    assert metadata["source"] == "direct_private_gt_trigger"


def test_runtime_spec_accepts_direct_private_trigger(tmp_path):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {"trigger": "./target -runs=1 /gt/poc"},
    }))

    command, metadata = load_runtime_spec(tmp_path)

    assert command == "./target -runs=1 /gt/poc"
    assert metadata["source"] == "direct_private_gt_trigger"


def test_runtime_readiness_accepts_cloneable_source(tmp_path, monkeypatch):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {"trigger": "./build.sh './target /gt/poc'"},
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
    monkeypatch.setattr(
        "harness_runtime.openhands.local.hydrate_runtime_workspace",
        lambda sample_dir: {
            "prepared": True,
            "hydrated": True,
            "source": str(sample_dir / "_work" / "src"),
        },
    )
    monkeypatch.setattr(
        "harness_runtime.openhands.local.compile_runtime_spec",
        lambda sample_dir, require_artifacts: RuntimeSpec(
            sample_id=sample_dir.name,
            backend="local_workspace",
            image="gt-memory-env:latest",
            workdir="/gt/_work/src",
            executable="./target",
            arguments=["{poc}"],
            environment={},
            input_placeholder="{poc}",
            source="ground_truth.poc.trigger",
        ),
    )

    readiness = check_runtime_readiness(tmp_path)

    assert readiness["ready"] is True
    assert readiness["source_strategy"] == "hydrated_from_sample_info"
    assert readiness["hydration"]["runtime_spec_ready"] is True
    assert readiness["runtime_source"] == "ground_truth.poc.trigger"
    assert readiness["runtime_image"] == "gt-memory-env:latest"
    assert readiness["runtime_workdir"] == "/gt/_work/src"
    assert readiness["required_images"] == ["gt-memory-env:latest", "alpine:3.23"]


def test_runtime_readiness_accepts_packaged_runtime_workspace(tmp_path, monkeypatch):
    sample_dir = tmp_path / "secbench_case"
    sample_dir.mkdir()
    (sample_dir / "build.sh").write_text("IMAGE=gt-memory-env:latest\n")
    (sample_dir / "runtime_spec.json").write_text(json.dumps({
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
    (sample_dir / "sample_info.json").write_text(json.dumps({
        "repo": "https://example.test/project.git",
        "vulnerable_commit": "deadbeef",
    }))
    target_dir = sample_dir / "_work" / "src" / "bin"
    target_dir.mkdir(parents=True)
    target = target_dir / "target"
    target.write_bytes(b"\x7fELF")
    target.chmod(0o755)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""),
    )

    readiness = check_runtime_readiness(sample_dir)

    assert readiness["ready"] is True
    assert readiness["hydration"]["runtime_spec_ready"] is True
    assert target.is_file()


def test_copy_source_includes_runtime_spec_root_artifact(tmp_path, monkeypatch):
    sample_dir = tmp_path / "secbench_case"
    sample_dir.mkdir()
    (sample_dir / "sample_info.json").write_text(json.dumps({
        "repo": "https://example.test/project.git",
        "vulnerable_commit": "deadbeef",
    }))
    (sample_dir / "runtime_spec.json").write_text(json.dumps({
        "sample_id": "secbench_case",
        "backend": "local_workspace",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "/gt/root_fuzzer",
        "arguments": ["{poc}"],
        "environment": {},
        "input_placeholder": "{poc}",
        "source": "runtime_spec.json",
    }))
    src = sample_dir / "_work" / "src"
    src.mkdir(parents=True)
    (src / "main.c").write_text("int main(void) { return 0; }\n")
    root_fuzzer = sample_dir / "root_fuzzer"
    root_fuzzer.write_bytes(b"\x7fELF")
    root_fuzzer.chmod(0o755)
    monkeypatch.setattr(
        "harness_runtime.openhands.local.hydrate_runtime_workspace",
        lambda sample_dir: {"prepared": True, "reused": True, "source": str(src)},
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    copy_source(sample_dir, workspace, load_json(sample_dir / "sample_info.json"))

    assert (workspace / "_work" / "src" / "main.c").is_file()
    assert (workspace / "root_fuzzer").is_file()


def test_agent_visible_source_tree_replaces_runtime_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    src = workspace / "_work" / "src"
    src.mkdir(parents=True)
    (src / "parser.c").write_text("int parse(void) { return 0; }\n", encoding="utf-8")
    (src / "parser.o").write_text("object\n", encoding="utf-8")
    repo = workspace / "repo-vul"
    repo.mkdir()
    (repo / "src-vul").symlink_to("../_work/src")

    materialize_agent_visible_source_tree(workspace)

    repo_src = repo / "src-vul"
    assert repo_src.is_dir()
    assert not repo_src.is_symlink()
    assert (repo_src / "parser.c").is_file()
    assert not (repo_src / "parser.o").exists()


def test_runtime_spec_command_unwraps_env_before_rendering():
    command = command_from_runtime_spec({
        "backend": "local_workspace",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "env",
        "arguments": ["ASAN_OPTIONS=detect_leaks=0", "./target", "{poc}"],
        "environment": {"LD_LIBRARY_PATH": "/gt/_work/src/.libs"},
    }, "secbench_case")

    assert command == (
        "ASAN_OPTIONS=detect_leaks=0 LD_LIBRARY_PATH=/gt/_work/src/.libs "
        "./target /gt/poc"
    )


def test_runtime_readiness_rejects_missing_runtime_images(tmp_path, monkeypatch):
    (tmp_path / "ground_truth.json").write_text(json.dumps({
        "poc": {"trigger": "./build.sh './target /gt/poc'"},
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
    monkeypatch.setattr(
        "harness_runtime.openhands.local.hydrate_runtime_workspace",
        lambda sample_dir: {
            "prepared": True,
            "hydrated": True,
            "source": str(sample_dir / "_work" / "src"),
        },
    )
    monkeypatch.setattr(
        "harness_runtime.openhands.local.compile_runtime_spec",
        lambda sample_dir, require_artifacts: RuntimeSpec(
            sample_id=sample_dir.name,
            backend="local_workspace",
            image="gt-memory-env:latest",
            workdir="/gt/_work/src",
            executable="./target",
            arguments=["{poc}"],
            environment={},
            input_placeholder="{poc}",
            source="ground_truth.poc.trigger",
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
    import types

    docker_module = types.SimpleNamespace(
        from_env=lambda: client,
        errors=types.SimpleNamespace(),
    )
    monkeypatch.setitem(sys.modules, "docker", docker_module)
    monkeypatch.setitem(sys.modules, "docker.errors", docker_module.errors)

    assert bridge.url == "http://172.20.0.1:4321"


def test_local_bridge_decodes_timeout_bytes(tmp_path, monkeypatch):
    bridge = LocalExecutionBridge.__new__(LocalExecutionBridge)
    bridge.workspace = tmp_path
    bridge.runtime_workspace = tmp_path
    bridge.image = "gt-memory-env:latest"
    bridge.workdir = "/gt/_work/src"
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


def test_copy_source_hydrates_and_copies_public_runtime_scaffold(tmp_path, monkeypatch):
    sample_dir = tmp_path / "gt" / "secbench_case"
    sample_dir.mkdir(parents=True)
    (sample_dir / "oss_fuzz_build.sh").write_text("#!/usr/bin/env bash\n")
    (sample_dir / "harness_downloads").mkdir()
    (sample_dir / "harness_downloads" / "helper.c").write_text("int LLVMFuzzerTestOneInput();\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def hydrate(sample_path):
        assert sample_path == sample_dir
        source = sample_dir / "_work" / "src"
        source.mkdir(parents=True)
        (source / "parser.c").write_text("void parse(void) {}\n")
        return {"prepared": True, "hydrated": True, "source": str(source)}

    monkeypatch.setattr(
        "harness_runtime.openhands.local.hydrate_runtime_workspace",
        hydrate,
    )

    copy_source(sample_dir, workspace, {})

    assert (workspace / "_work" / "src" / "parser.c").is_file()
    assert (workspace / "repo-vul" / "src-vul").is_symlink()
    assert (workspace / "oss_fuzz_build.sh").is_file()
    assert (workspace / "harness_downloads" / "helper.c").is_file()


def test_trigger_requires_runtime_detector_evidence():
    assert runtime_triggered("ERROR: AddressSanitizer: heap-buffer-overflow", 1)
    assert not runtime_triggered("ordinary parser rejection", 1)


def test_local_complete_requires_checkpoint_analysis_and_manifest(tmp_path):
    result = tmp_path / "sample_1"
    attempt = result / "submissions" / "a1"
    attempt.mkdir(parents=True)
    (result / "checkpoint").mkdir()
    analysis = {
        "sample_id": "sample_1",
        "fine_trace": [{
            "step": 1,
            "file": "src/parser.c",
            "function": "parse",
            "line": 10,
            "var": "buf",
            "code": "parse(buf);",
            "note": "input reaches parser",
            "role": "source",
        }],
        "vuln_logic": {
            "source": {
                "file": "src/parser.c",
                "function": "parse",
                "line": 10,
                "operands": ["buf"],
            },
            "root_cause": {
                "file": "src/parser.c",
                "function": "parse",
                "line": 12,
                "operands": ["len", "cap"],
                "relation": {"op": "le", "left": "len", "right": "cap"},
            },
            "sink": {
                "file": "src/parser.c",
                "function": "parse",
                "line": 20,
                "operands": ["len", "cap"],
                "relation": {"op": "gt", "left": "len", "right": "cap"},
            },
            "propagation": [{
                "from": {
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 10,
                    "operands": ["buf"],
                },
                "to": {
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 20,
                    "operands": ["len"],
                },
                "type": "data",
                "via": ["len"],
            }],
        },
    }
    (result / "analysis.json").write_text(json.dumps(analysis))
    for name in (
        "poc.bin", "analysis.json", "result.json", "runtime_output.txt",
    ):
        (attempt / name).write_text(json.dumps(analysis) if name == "analysis.json" else "")
    (result / "manifest.json").write_text(json.dumps({
        "evaluation_protocol": "poc_analysis_artifact_per_submission_v3_local",
        "max_iter": 100,
        "status": "iteration_cap",
        "submission_attempts": [{"attempt_id": "a1"}],
    }))
    assert result_is_complete(result)


def test_extract_analysis_from_final_thought_log():
    artifact = {
        "sample_id": "sample_1",
        "fine_trace": [
            {
                "step": 1,
                "file": "src/parser.c",
                "function": "parse",
                "line": 10,
                "var": "buf",
                "code": "parse(buf);",
                "note": "input reaches parser",
                "role": "source",
            },
            {
                "step": 2,
                "file": "src/parser.c",
                "function": "parse",
                "line": 12,
                "var": "len",
                "code": "if (len <= cap)",
                "note": "length must stay within capacity",
                "role": "root_cause",
            },
            {
                "step": 3,
                "file": "src/parser.c",
                "function": "parse",
                "line": 20,
                "var": "buf[len]",
                "code": "buf[len] = 0;",
                "note": "write uses unchecked length",
                "role": "sink",
            },
        ],
        "vuln_logic": {
            "source": {
                "file": "src/parser.c",
                "function": "parse",
                "line": 10,
                "operands": ["buf"],
            },
            "root_cause": {
                "file": "src/parser.c",
                "function": "parse",
                "line": 12,
                "operands": ["len", "cap"],
                "relation": {"op": "le", "left": "len", "right": "cap"},
            },
            "sink": {
                "file": "src/parser.c",
                "function": "parse",
                "line": 20,
                "operands": ["len", "cap"],
                "relation": {"op": "gt", "left": "len", "right": "cap"},
            },
            "propagation": [
                {
                    "from": {
                        "file": "src/parser.c",
                        "function": "parse",
                        "line": 10,
                        "operands": ["buf"],
                    },
                    "to": {
                        "file": "src/parser.c",
                        "function": "parse",
                        "line": 20,
                        "operands": ["len"],
                    },
                    "type": "data",
                    "via": ["len"],
                },
            ],
        },
    }
    log = (
        "AgentFinishAction(final_thought="
        + repr(json.dumps(artifact))
        + ", task_completed=None)"
    )

    recovered = _extract_analysis_from_text(log, "sample_1")

    assert recovered is not None
    assert json.loads(recovered) == artifact



def test_normalize_runtime_build_command_copies_local_source_tree_without_git():
    command = 'rm -rf "$WORK/libical" "$SRC/libical"; git clone --quiet --no-hardlinks "$SRC" "$WORK/libical"; mv "$WORK/libical" "$SRC/libical"'

    normalized = normalize_runtime_build_command(command)

    assert 'git clone --quiet --no-hardlinks "$SRC"' not in normalized
    assert 'mkdir -p "$WORK/libical"; cp -a "$SRC/." "$WORK/libical"/' in normalized
    assert normalized.endswith('mv "$WORK/libical" "$SRC/libical"')

def test_runtime_build_artifacts_are_hidden_from_agent_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("build.sh", "oss_fuzz_build.sh", "oss_fuzz_setup.sh", "runtime_build.json"):
        (workspace / name).write_text("runtime helper\n", encoding="utf-8")
    (workspace / "runtime_support").mkdir()
    (workspace / "runtime_support" / "build_runtime.sh").write_text("echo build\n", encoding="utf-8")
    (workspace / ".runtime_build_logs").mkdir()
    (workspace / ".runtime_build_logs" / "build_1.log").write_text("ok\n", encoding="utf-8")
    (workspace / "oss_fuzz_project").mkdir()
    (workspace / "oss_fuzz_project" / "build.sh").write_text("echo build\n", encoding="utf-8")
    (workspace / "oss_fuzz_project" / "Dockerfile").write_text("FROM base\n", encoding="utf-8")
    (workspace / "_work").mkdir()
    (workspace / "_work" / "build.sh").write_text("echo build\n", encoding="utf-8")
    (workspace / "_work" / "Dockerfile").write_text("FROM base\n", encoding="utf-8")
    (workspace / "_out").mkdir()
    (workspace / "_out" / "target").write_text("binary\n", encoding="utf-8")
    (workspace / "fuzz_eval").write_text("binary\n", encoding="utf-8")

    result = scrub_agent_visible_runtime_build_artifacts(workspace, {"executable": "/gt/fuzz_eval"})

    assert not result["errors"]
    for name in (
        "build.sh",
        "oss_fuzz_build.sh",
        "oss_fuzz_setup.sh",
        "runtime_build.json",
        "runtime_support",
        ".runtime_build_logs",
        "_out",
        "oss_fuzz_project/build.sh",
        "oss_fuzz_project/Dockerfile",
        "_work",
        "_work/build.sh",
        "_work/Dockerfile",
        "fuzz_eval",
    ):
        assert not (workspace / name).exists()
    assert not (workspace / "_out" / "target").exists()
    assert (workspace / "oss_fuzz_project").is_dir()
    assert not (workspace / "_work").exists()
    assert not (workspace / ".benchmark_runner").exists()
