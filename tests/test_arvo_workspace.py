import json
import subprocess

import pytest

from gt_generation.gt_toolkit import arvo_workspace, prepare
from gt_generation.gt_toolkit.assertions import assertion_content_hash, freeze_spec


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_prepare_pulls_only_vulnerable_arvo_image(tmp_path, monkeypatch):
    pulled = []
    monkeypatch.setattr(prepare, "_pull", lambda image: pulled.append(image) or True)

    def fake_sh(cmd, timeout=None):
        if cmd[:2] == ["docker", "create"]:
            return _proc(stdout="container-id\n")
        if cmd[:3] == ["docker", "run", "--rm"]:
            return _proc(stdout="/out/fuzz_target /tmp/poc\n")
        return _proc()

    monkeypatch.setattr(prepare, "_sh", fake_sh)
    prepare._prepare_arvo(
        {"sample_id": "arvo_42", "source_dataset": "ARVO"}, tmp_path
    )

    assert pulled == ["n132/arvo:42-vul"]


def test_detect_arvo_target_reads_run_command(monkeypatch):
    monkeypatch.setattr(
        prepare,
        "_sh",
        lambda cmd, timeout=None: _proc(
            stdout='if true; then\n  /out/fuzz_format_sav /tmp/poc\nfi\n'
        ),
    )
    assert prepare._detect_arvo_target("image") == "fuzz_format_sav"


def test_source_root_is_detected_from_src_git_checkout(tmp_path, monkeypatch):
    # ARVO patch.diff is often an unrelated commit, so the source root is resolved from
    # the project's own /src git checkout, independent of patch.diff.
    (tmp_path / "patch.diff").write_text(
        "diff --git a/unrelated/other.c b/unrelated/other.c\n", encoding="utf-8"
    )
    context = {"container": "workspace"}
    commands = []
    monkeypatch.setattr(arvo_workspace, "_context", lambda result_dir: context)

    def fake_exec(container, command, timeout=3600):
        commands.append(command)
        if "-name .git" in command:
            return _proc(stdout="/src/skia\n")
        if command.startswith("git -C"):
            return _proc(stdout="/src/skia\n")
        return _proc()

    monkeypatch.setattr(arvo_workspace, "_docker_exec", fake_exec)

    assert arvo_workspace._source_root(tmp_path) == "/src/skia"
    assert any("-name .git" in command for command in commands)
    assert json.loads((tmp_path / "arvo_workspace.json").read_text())["source_root"] == "/src/skia"


def test_fixed_compile_is_target_level_incremental_not_full(tmp_path, monkeypatch):
    context = {
        "container": "gt-arvo_42-workspace",
        "target": "fuzz_format_sav",
        "vul_image": "n132/arvo:42-vul",
        "fix_image": "n132/arvo:42-fix",
    }
    commands = []
    monkeypatch.setattr(arvo_workspace, "_context", lambda result_dir: context)
    monkeypatch.setattr(
        arvo_workspace,
        "_require_execution_authorization",
        lambda result_dir, runtime_disambiguation: {},
    )
    monkeypatch.setattr(arvo_workspace, "_verify_source_fingerprint", lambda result_dir: None)
    monkeypatch.setattr(arvo_workspace, "_source_root", lambda result_dir: "/src/readstat")

    def fake_exec(container, command, timeout=3600):
        commands.append((container, command))
        return _proc()

    monkeypatch.setattr(arvo_workspace, "_docker_exec", fake_exec)
    monkeypatch.setattr(arvo_workspace, "_write_log", lambda *args: None)
    monkeypatch.setattr(arvo_workspace, "_update_state", lambda *args, **kwargs: {})

    assert arvo_workspace.compile_fixed(tmp_path) == 0
    assert len(commands) == 1
    assert commands[0][0] == "gt-arvo_42-workspace"
    assert "/src/readstat/out/*/fuzz_format_sav" in commands[0][1]
    assert "ninja -C \"$build_dir\" fuzz_format_sav" in commands[0][1]
    assert "make -C /src/readstat fuzz_format_sav" in commands[0][1]
    assert "/bin/arvo compile" not in commands[0][1]


def test_instrumented_vulnerable_compile_reuses_configured_target(tmp_path, monkeypatch):
    context = {
        "container": "gt-arvo_42-workspace",
        "target": "fuzz_format_sav",
        "vul_image": "n132/arvo:42-vul",
        "fix_image": "n132/arvo:42-fix",
    }
    commands = []
    monkeypatch.setattr(arvo_workspace, "_context", lambda result_dir: context)
    monkeypatch.setattr(
        arvo_workspace,
        "_require_execution_authorization",
        lambda result_dir, runtime_disambiguation: {},
    )
    monkeypatch.setattr(arvo_workspace, "_verify_source_fingerprint", lambda result_dir: None)
    monkeypatch.setattr(arvo_workspace, "_source_root", lambda result_dir: "/src/readstat")
    monkeypatch.setattr(
        arvo_workspace,
        "_docker_exec",
        lambda container, command, timeout=3600: commands.append(command) or _proc(),
    )
    monkeypatch.setattr(arvo_workspace, "_write_log", lambda *args: None)
    monkeypatch.setattr(arvo_workspace, "_update_state", lambda *args, **kwargs: {})

    assert arvo_workspace.compile_target(tmp_path, version="vulnerable") == 0
    assert len(commands) == 1
    assert "/src/readstat/out/*/fuzz_format_sav" in commands[0]
    assert "ninja -C \"$build_dir\" fuzz_format_sav" in commands[0]
    assert "/bin/arvo compile" not in commands[0]


def test_target_compile_falls_back_without_agent_polling(tmp_path, monkeypatch):
    context = {
        "container": "gt-arvo_42-workspace",
        "target": "fuzz_format_sav",
        "vul_image": "n132/arvo:42-vul",
        "fix_image": "n132/arvo:42-fix",
    }
    commands = []
    monkeypatch.setattr(arvo_workspace, "_context", lambda result_dir: context)
    monkeypatch.setattr(
        arvo_workspace,
        "_require_execution_authorization",
        lambda result_dir, runtime_disambiguation: {},
    )
    monkeypatch.setattr(arvo_workspace, "_verify_source_fingerprint", lambda result_dir: None)
    monkeypatch.setattr(arvo_workspace, "_source_root", lambda result_dir: "/src/readstat")

    def fake_exec(container, command, timeout=3600):
        commands.append(command)
        return _proc(returncode=2 if command != "/bin/arvo compile" else 0)

    monkeypatch.setattr(arvo_workspace, "_docker_exec", fake_exec)
    monkeypatch.setattr(arvo_workspace, "_write_log", lambda *args: None)
    monkeypatch.setattr(arvo_workspace, "_update_state", lambda *args, **kwargs: {})

    assert arvo_workspace.compile_target(tmp_path, version="vulnerable") == 0
    assert len(commands) == 2
    assert "/src/readstat/out/*/fuzz_format_sav" in commands[0]
    assert commands[1] == "/bin/arvo compile"


def test_cleanup_removes_local_working_copies(tmp_path, monkeypatch):
    (tmp_path / "_work" / "src").mkdir(parents=True)
    (tmp_path / "_work" / "src" / "source.c").write_text("int x;")
    (tmp_path / "arvo_workspace").mkdir()
    (tmp_path / "arvo_workspace" / "compile.log").write_text("done")
    context = {
        "container": "gt-arvo_42-workspace",
        "vul_image": "n132/arvo:42-vul",
        "fix_image": "n132/arvo:42-fix",
    }
    monkeypatch.setattr(arvo_workspace, "_context", lambda result_dir: context)
    monkeypatch.setattr(arvo_workspace, "_run", lambda *args, **kwargs: _proc())
    monkeypatch.setattr(arvo_workspace, "_update_state", lambda *args, **kwargs: {})

    assert arvo_workspace.cleanup(tmp_path, remove_images=False) == 0
    assert not (tmp_path / "_work").exists()
    assert not (tmp_path / "arvo_workspace").exists()


def test_switch_fixed_preserves_untracked_build_outputs(tmp_path, monkeypatch):
    context = {"container": "workspace"}
    commands = []
    monkeypatch.setattr(arvo_workspace, "_context", lambda result_dir: context)
    monkeypatch.setattr(arvo_workspace, "_require_frozen_spec", lambda result_dir: {})
    monkeypatch.setattr(arvo_workspace, "_source_root", lambda result_dir: "/src/readstat")
    monkeypatch.setattr(arvo_workspace, "_run", lambda *args, **kwargs: _proc())
    monkeypatch.setattr(
        arvo_workspace,
        "_docker_exec",
        lambda container, command, timeout=3600: commands.append(command) or _proc(),
    )
    monkeypatch.setattr(arvo_workspace, "_write_log", lambda *args: None)
    monkeypatch.setattr(arvo_workspace, "_update_state", lambda *args, **kwargs: {})

    assert arvo_workspace.switch_fixed(tmp_path, tmp_path / "patch.diff") == 0
    assert "git -C /src/readstat reset --hard HEAD" in commands[0]
    assert "git -C /src/readstat apply" in commands[0]
    assert "git clean" not in commands[0]


def test_workspace_rejects_execution_before_exact_spec_is_frozen(tmp_path):
    with pytest.raises(RuntimeError, match="locked until"):
        arvo_workspace._require_frozen_spec(tmp_path)

    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [{
            "id": "required.bound",
            "invariants": ["root.bound"],
            "kind": "required",
            "at": "decision",
            "check": ["ge", "$count", -3],
            "protects": "read",
        }],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    spec_path = tmp_path / "candidate_assertions.json"
    spec_path.write_text(json.dumps(spec) + "\n")
    freeze_spec(spec_path, tmp_path / ".assertion_spec_frozen.json")

    assert arvo_workspace._require_frozen_spec(tmp_path)["content_hash"] == spec["content_hash"]

    spec_path.write_text(spec_path.read_text() + "\n")
    with pytest.raises(RuntimeError, match="changed after freeze"):
        arvo_workspace._require_frozen_spec(tmp_path)


def test_runtime_disambiguation_authorization_uses_existing_control_files(tmp_path):
    arvo_workspace._write(
        tmp_path / "run_flags.json", {"runtime_disambiguation": True}
    )
    arvo_workspace._write(
        tmp_path / "trace_feedback.json",
        {
            "needs_runtime_disambiguation": True,
            "observe": "correlate producer and consumer offsets",
        },
    )

    assert (
        arvo_workspace._require_execution_authorization(
            tmp_path, runtime_disambiguation=True
        )
        is None
    )
    with pytest.raises(RuntimeError, match="locked until"):
        arvo_workspace._require_execution_authorization(
            tmp_path, runtime_disambiguation=False
        )

    arvo_workspace._write(
        tmp_path / "trace_feedback.json",
        {"needs_runtime_disambiguation": True, "observe": ""},
    )
    with pytest.raises(RuntimeError, match="no observe"):
        arvo_workspace._require_runtime_disambiguation(tmp_path)


def test_ensure_vulnerable_workspace_recreates_only_when_missing(tmp_path, monkeypatch):
    context = {
        "container": "workspace",
        "vul_image": "n132/arvo:42-vul",
    }
    calls = []
    monkeypatch.setattr(arvo_workspace, "_context", lambda result_dir: context)
    monkeypatch.setattr(
        arvo_workspace,
        "_run",
        lambda cmd, timeout=3600: calls.append(cmd) or _proc(
            returncode=1 if cmd[:2] == ["docker", "inspect"] else 0
        ),
    )
    monkeypatch.setattr(arvo_workspace, "create", lambda result_dir: 0)
    monkeypatch.setattr(arvo_workspace, "compile_vulnerable", lambda result_dir: 0)

    assert arvo_workspace.ensure_vulnerable_workspace(tmp_path) == 0
    assert ["docker", "pull", "n132/arvo:42-vul"] in calls


def test_workspace_rejects_unpersisted_container_source_edit(tmp_path, monkeypatch):
    arvo_workspace._write(
        tmp_path / "arvo_workspace.json",
        {"instrumentation_source_sha256": "sha256:expected"},
    )
    monkeypatch.setattr(
        arvo_workspace, "_source_fingerprint", lambda result_dir: "sha256:changed"
    )

    with pytest.raises(RuntimeError, match="outside the persisted instrumentation patch"):
        arvo_workspace._verify_source_fingerprint(tmp_path)


def test_instrumentation_patch_must_use_fixed_persisted_result_path(tmp_path):
    arvo_workspace._write(
        tmp_path / "arvo_workspace.json", {"phase": "vulnerable_compiled"}
    )
    expected = tmp_path / "vulnerable-instrumentation.patch"
    expected.write_text("diff --git a/a b/a\n")

    assert arvo_workspace._require_persisted_instrumentation_patch(
        tmp_path, expected
    ) == expected.resolve()

    temporary = tmp_path / "_work" / "instrumentation.patch"
    temporary.parent.mkdir()
    temporary.write_text(expected.read_text())
    with pytest.raises(RuntimeError, match="persisted exactly"):
        arvo_workspace._require_persisted_instrumentation_patch(tmp_path, temporary)
