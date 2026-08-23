import json
from pathlib import Path

from gt_generation.gt_toolkit import portability


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _stage01_result(tmp_path: Path) -> Path:
    result = tmp_path / "secbench_case"
    result.mkdir()
    _write_json(result / "sample_info.json", {
        "sample_id": result.name,
        "repo": "https://example.invalid/project.git",
        "vulnerable_commit": "vulnerable",
        "fix_commit": "fixed",
    })
    _write_json(result / "reproduction_report.json", {
        "vulnerable_reproduced": True,
        "matches_issue": True,
        "setup_command": "/gt/build.sh 'set -euo pipefail\nmake -j8'",
        "command": "/gt/_work/src/bin/target /gt/poc",
    })
    (result / "build.sh").write_text(
        "#!/usr/bin/env bash\nIMAGE=gt-memory-env:latest\n", encoding="utf-8"
    )
    (result / "build.sh").chmod(0o755)
    (result / "poc").write_bytes(b"poc")
    (result / "sanitizer_trace.txt").write_text(
        "ERROR: AddressSanitizer: heap-buffer-overflow\n", encoding="utf-8"
    )
    return result


def test_freeze_runtime_contract_writes_lightweight_recipe_and_spec(
    tmp_path, monkeypatch
):
    result = _stage01_result(tmp_path)

    class FakeSpec:
        def to_dict(self):
            return {
                "sample_id": result.name,
                "backend": "local_workspace",
                "image": "gt-memory-env:latest",
                "workdir": "/gt/_work/src",
                "executable": "./bin/target",
                "arguments": ["{poc}"],
                "environment": {},
                "input_placeholder": "{poc}",
                "source": "reproduction_report.json",
            }

    class FakeRuntimeSpec:
        @staticmethod
        def compile_runtime_spec(*_args, **_kwargs):
            return FakeSpec()

    monkeypatch.setattr(portability, "_runtime_spec_module", lambda: FakeRuntimeSpec)

    report = portability.freeze_runtime_contract(result)

    assert report["ok"] is True
    recipe = json.loads((result / "runtime_build.json").read_text())
    assert recipe["commands"][0]["command"] == "set -euo pipefail\nmake -j8"
    assert recipe["commands"][0]["environment"] == {"GT_BUILD_JOBS": "1"}
    spec = json.loads((result / "runtime_spec.json").read_text())
    assert spec["arguments"] == ["{poc}"]
    assert spec["build_commands"] == ["set -euo pipefail\nmake -j8"]
    assert spec["source_repo"] == "https://example.invalid/project.git"
    assert spec["source_commit"] == "vulnerable"
    assert (result / "runtime_materials.json").is_file()


def test_frozen_evaluator_build_command_drops_source_mutation(tmp_path, monkeypatch):
    result = _stage01_result(tmp_path)
    report = json.loads((result / "reproduction_report.json").read_text())
    report["setup_command"] = (
        "/gt/build.sh 'set -euo pipefail\n"
        "git reset --hard vulnerable\n"
        "git clean -fdx\n"
        "make'"
    )
    _write_json(result / "reproduction_report.json", report)

    class FakeSpec:
        def to_dict(self):
            return {
                "sample_id": result.name, "backend": "local_workspace",
                "image": "gt-memory-env:latest", "workdir": "/gt/_work/src",
                "executable": "./target", "arguments": ["{poc}"],
                "environment": {}, "input_placeholder": "{poc}",
                "source": "test",
            }

    class FakeRuntimeSpec:
        compile_runtime_spec = staticmethod(lambda *_args, **_kwargs: FakeSpec())

    monkeypatch.setattr(portability, "_runtime_spec_module", lambda: FakeRuntimeSpec)

    frozen = portability.freeze_runtime_contract(result)
    spec = json.loads((result / "runtime_spec.json").read_text())

    assert frozen["ok"] is True
    assert spec["build_commands"] == ["set -euo pipefail\nmake"]


def test_frozen_evaluator_keeps_commands_after_inline_checkout(tmp_path, monkeypatch):
    result = _stage01_result(tmp_path)
    report = json.loads((result / "reproduction_report.json").read_text())
    report["setup_command"] = (
        "git checkout vulnerable && rm -rf build && "
        "make CC=clang CFLAGS='-fsanitize=address'"
    )
    _write_json(result / "reproduction_report.json", report)

    class FakeSpec:
        def to_dict(self):
            return {
                "sample_id": result.name, "backend": "local_workspace",
                "image": "gt-memory-env:latest", "workdir": "/gt/_work/src",
                "executable": "./target", "arguments": ["{poc}"],
                "environment": {}, "input_placeholder": "{poc}",
                "source": "test",
            }

    class FakeRuntimeSpec:
        compile_runtime_spec = staticmethod(lambda *_args, **_kwargs: FakeSpec())

    monkeypatch.setattr(portability, "_runtime_spec_module", lambda: FakeRuntimeSpec)

    frozen = portability.freeze_runtime_contract(result)
    spec = json.loads((result / "runtime_spec.json").read_text())

    assert frozen["ok"] is True
    assert spec["build_commands"] == [
        "rm -rf build && make CC=clang CFLAGS='-fsanitize=address'"
    ]


def test_portable_copy_excludes_work_outputs_archives_and_reports(tmp_path):
    result = _stage01_result(tmp_path)
    _write_json(result / "runtime_build.json", {
        "schema_version": "gt-runtime-build-v1",
        "sample_id": result.name,
        "commands": [{"command": "bash /gt/helper.sh", "run_as_root": False}],
    })
    _write_json(result / "runtime_spec.json", {
        "sample_id": result.name,
        "backend": "local_workspace",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "./bin/target",
        "arguments": ["{poc}"],
        "environment": {},
        "input_placeholder": "{poc}",
        "source": "test",
    })
    (result / "helper.sh").write_text("make\n")
    (result / "runtime_work.tar.gz").write_bytes(b"archive")
    (result / "_work" / "src").mkdir(parents=True)
    (result / "_work" / "src" / "binary").write_bytes(b"binary")
    (result / "build.log").write_text("log")
    destination = tmp_path / "copy"

    portability._copy_portable_materials(result, destination)

    assert (destination / "sample_info.json").is_file()
    assert (destination / "runtime_build.json").is_file()
    assert (destination / "helper.sh").is_file()
    assert not (destination / "_work").exists()
    assert not (destination / "runtime_work.tar.gz").exists()
    assert not (destination / "build.log").exists()


def test_portable_copy_includes_oss_fuzz_context_only_when_recipe_uses_it(tmp_path):
    result = _stage01_result(tmp_path)
    _write_json(result / "runtime_spec.json", {
        "sample_id": result.name,
        "backend": "local_workspace",
        "image": "gt-memory-env:latest",
        "workdir": "/gt/_work/src",
        "executable": "./target",
        "arguments": ["{poc}"],
        "environment": {},
        "input_placeholder": "{poc}",
        "source": "test",
    })
    (result / "oss_fuzz_setup.sh").write_text("true\n")
    (result / "oss_fuzz_src" / "helper").mkdir(parents=True)
    (result / "oss_fuzz_src" / "helper" / "input.txt").write_text("needed")
    _write_json(result / "runtime_build.json", {
        "schema_version": "gt-runtime-build-v1",
        "sample_id": result.name,
        "commands": [{"command": "bash /gt/oss_fuzz_setup.sh && make"}],
    })

    paths = {
        path.relative_to(result).as_posix()
        for path in portability.portable_material_paths(result)
    }

    assert "oss_fuzz_setup.sh" in paths
    assert "oss_fuzz_src" in paths

    _write_json(result / "runtime_build.json", {
        "schema_version": "gt-runtime-build-v1",
        "sample_id": result.name,
        "commands": [{"command": "make"}],
    })
    paths = {
        path.relative_to(result).as_posix()
        for path in portability.portable_material_paths(result)
    }
    assert "oss_fuzz_setup.sh" not in paths
    assert "oss_fuzz_src" not in paths


def test_portable_materials_include_referenced_top_level_directory(tmp_path):
    result = _stage01_result(tmp_path)
    (result / "custom_helper" / "config").mkdir(parents=True)
    (result / "custom_helper" / "config" / "target.ini").write_text("x=1")
    _write_json(result / "runtime_build.json", {
        "schema_version": "gt-runtime-build-v1",
        "sample_id": result.name,
        "commands": [{"command": "tool --config /gt/custom_helper/config/target.ini"}],
    })

    paths = {
        path.relative_to(result).as_posix()
        for path in portability.portable_material_paths(result)
    }

    assert "custom_helper" in paths


def test_portable_materials_exclude_generated_logs_but_keep_text_inputs(tmp_path):
    result = _stage01_result(tmp_path)
    (result / "build_vulnerable.log").write_text("compiler output")
    (result / "config.txt").write_text("required input")
    _write_json(result / "runtime_build.json", {
        "schema_version": "gt-runtime-build-v1",
        "sample_id": result.name,
        "commands": [{
            "command": (
                "tool --config /gt/config.txt "
                "> /gt/build_vulnerable.log 2>&1"
            )
        }],
    })

    paths = {
        path.relative_to(result).as_posix()
        for path in portability.portable_material_paths(result)
    }

    assert "config.txt" in paths
    assert "build_vulnerable.log" not in paths


def test_contract_rejects_result_root_path_escape(tmp_path, monkeypatch):
    result = _stage01_result(tmp_path)
    report = json.loads((result / "reproduction_report.json").read_text())
    report["setup_command"] = "/gt/build.sh 'bash /gt/../host-helper.sh'"
    _write_json(result / "reproduction_report.json", report)

    class FakeSpec:
        def to_dict(self):
            return {
                "sample_id": result.name,
                "backend": "local_workspace",
                "image": "gt-memory-env:latest",
                "workdir": "/gt/_work/src",
                "executable": "./target",
                "arguments": ["{poc}"],
                "environment": {},
                "input_placeholder": "{poc}",
                "source": "test",
            }

    class FakeRuntimeSpec:
        compile_runtime_spec = staticmethod(lambda *_args, **_kwargs: FakeSpec())

    monkeypatch.setattr(portability, "_runtime_spec_module", lambda: FakeRuntimeSpec)

    frozen = portability.freeze_runtime_contract(result)

    assert frozen["ok"] is False
    assert "unsafe result-root path" in frozen["reason"]


def test_contract_rejects_missing_result_root_build_material(tmp_path, monkeypatch):
    result = _stage01_result(tmp_path)
    report = json.loads((result / "reproduction_report.json").read_text())
    report["setup_command"] = "/gt/build.sh 'bash /gt/missing_helper.sh'"
    _write_json(result / "reproduction_report.json", report)

    class FakeSpec:
        def to_dict(self):
            return {
                "sample_id": result.name,
                "backend": "local_workspace",
                "image": "gt-memory-env:latest",
                "workdir": "/gt/_work/src",
                "executable": "./target",
                "arguments": ["{poc}"],
                "environment": {},
                "input_placeholder": "{poc}",
                "source": "test",
            }

    class FakeRuntimeSpec:
        compile_runtime_spec = staticmethod(lambda *_args, **_kwargs: FakeSpec())

    monkeypatch.setattr(portability, "_runtime_spec_module", lambda: FakeRuntimeSpec)

    frozen = portability.freeze_runtime_contract(result)

    assert frozen["ok"] is False
    assert "/gt/missing_helper.sh" in frozen["reason"]


def test_portability_gate_requires_all_four_oracles(tmp_path, monkeypatch):
    result = _stage01_result(tmp_path)
    monkeypatch.setattr(
        portability,
        "freeze_runtime_contract",
        lambda _path: {"ok": True},
    )
    monkeypatch.setattr(portability, "_copy_portable_materials", lambda *_args: None)
    calls = iter([
        {"ok": True},
        {"ok": True},
    ])
    monkeypatch.setattr(portability, "_build_side", lambda *_args: next(calls))
    runs = iter([
        {
            "finding_present": True,
            "finding_signature": {
                "sanitizer": "AddressSanitizer",
                "crash_type": "heap-buffer-overflow",
            },
        },
        {"execution_valid": True, "finding_present": False},
    ])
    monkeypatch.setattr(portability, "_run_frozen_spec", lambda *_args: next(runs))

    report = portability.run_portability_gate(result, timeout=1)

    assert report["vulnerable_build_ok"] is True
    assert report["vulnerable_triggered"] is True
    assert report["fixed_build_ok"] is True
    assert report["fixed_not_triggered"] is True
    assert report["runtime_portable"] is True
    assert portability.portability_gate_passes(result) is True


def test_portability_gate_rejects_fixed_execution_failure(tmp_path, monkeypatch):
    result = _stage01_result(tmp_path)
    monkeypatch.setattr(portability, "freeze_runtime_contract", lambda _path: {"ok": True})
    monkeypatch.setattr(portability, "_copy_portable_materials", lambda *_args: None)
    monkeypatch.setattr(portability, "_build_side", lambda *_args: {"ok": True})
    runs = iter([
        {
            "finding_present": True,
            "finding_signature": {
                "sanitizer": "AddressSanitizer",
                "crash_type": "heap-buffer-overflow",
            },
        },
        {"execution_valid": False, "finding_present": False},
    ])
    monkeypatch.setattr(portability, "_run_frozen_spec", lambda *_args: next(runs))

    report = portability.run_portability_gate(result, timeout=1)

    assert report["fixed_not_triggered"] is False
    assert report["runtime_portable"] is False


def test_materialize_stage01_portability_removes_generated_workspace_on_success(
    tmp_path, monkeypatch
):
    result = _stage01_result(tmp_path)
    (result / "_work" / "src").mkdir(parents=True)
    (result / "_out").mkdir()
    (result / "runtime_build_logs").mkdir()
    (result / "runtime_work.tar.gz").write_bytes(b"legacy")
    (result / "runtime_work.tar.gz.part-000").write_bytes(b"legacy part")
    _write_json(result / "runtime_work_manifest.json", {"archive": "runtime_work.tar.gz"})
    monkeypatch.setattr(
        portability,
        "run_portability_gate",
        lambda *_args, **_kwargs: {"runtime_portable": True},
    )

    report = portability.materialize_stage01_portability(result, timeout=1)

    assert report["runtime_portable"] is True
    assert not (result / "_work").exists()
    assert not (result / "_out").exists()
    assert not (result / "runtime_build_logs").exists()
    assert not (result / "runtime_work.tar.gz").exists()
    assert not (result / "runtime_work.tar.gz.part-000").exists()
    assert not (result / "runtime_work_manifest.json").exists()
