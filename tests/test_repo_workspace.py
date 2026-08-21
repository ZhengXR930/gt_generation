import subprocess
import json

from gt_generation.gt_toolkit import repo_workspace


def test_command_masks_failure_operators():
    assert repo_workspace.command_masks_failures("bash /gt/build.sh || true")
    assert repo_workspace.command_masks_failures("bash /gt/build.sh || true'")
    assert repo_workspace.command_masks_failures("set +e; ninja")
    assert not repo_workspace.command_masks_failures("set -euo pipefail\nninja")


def test_setup_replay_removes_recorded_checkout_that_would_erase_patch():
    setup = """set -euo pipefail
cd /gt/_work/src
git reset --hard 1111111
git checkout --force 1111111
export CC=clang CXX=clang++
ninja
"""

    replay = repo_workspace._setup_command_for_replay(
        setup,
        "vulnerable",
        "1111111",
        "2222222",
    )

    assert "git reset" not in replay
    assert "git checkout" not in replay
    assert "export CC=clang CXX=clang++" in replay
    assert "ninja" in replay


def test_recorded_build_command_unwraps_prefixed_env_assignment():
    recorded = (
        "GT_BUILD_AS_ROOT=1 /tmp/result/build.sh 'set -euo pipefail\n"
        "cd /gt/_work/src\n"
        "ninja'"
    )

    assert repo_workspace._inner_from_recorded_build_command(recorded) == (
        "set -euo pipefail\ncd /gt/_work/src\nninja"
    )
    assert repo_workspace._recorded_command_requires_root(
        recorded,
        repo_workspace._inner_from_recorded_build_command(recorded),
    )


def test_recorded_build_command_unwraps_container_build_path():
    recorded = "/gt/build.sh 'cd /gt/_work/src && ./target /gt/poc'"

    assert repo_workspace._inner_from_recorded_build_command(recorded) == (
        "cd /gt/_work/src && ./target /gt/poc"
    )


def test_clean_expectation_accepts_safe_non_sanitizer_error():
    proc = subprocess.CompletedProcess(
        args=["target"],
        returncode=1,
        stdout="",
        stderr="error: invalid conversion specifier '%'\n",
    )

    assert repo_workspace._trace_result(proc) == "error"
    assert not repo_workspace._crashed(proc)
    assert repo_workspace._expectation_matches("clean", "error")


def test_preflight_report_rejects_masked_compile_failure(tmp_path):
    apply_proc = subprocess.CompletedProcess(
        args=["build.sh"],
        returncode=0,
        stdout="",
        stderr="",
    )
    compile_proc = subprocess.CompletedProcess(
        args=["build.sh"],
        returncode=0,
        stdout=(
            "FAILED: code/CMakeFiles/target.dir/file.cpp.o\n"
            "error: use of undeclared identifier 'iFileSize'\n"
            "ninja: build stopped: subcommand failed.\n"
        ),
        stderr="",
    )

    result_dir = tmp_path
    (result_dir / "field_bindings.json").write_text(
        json.dumps({"bindings": {}}),
        encoding="utf-8",
    )

    report = repo_workspace._report_template(
        result_dir=result_dir,
        version="vulnerable",
        target_commit="1111111",
        patch=repo_workspace.Path(__file__),
        spec={"sample_id": "sample", "content_hash": "sha256:plan"},
        apply_proc=apply_proc,
        compile_proc=compile_proc,
        track="repo/secbench",
        setup_masks_failures=True,
    )

    assert report["ok"] is False
    assert report["check"]["setup_masks_failures"] is True
    assert any("FAILED:" in item for item in report["check"]["compile_failure_markers"])


def test_preflight_report_rejects_hardcoded_required_runtime_field(tmp_path):
    patch = tmp_path / "vulnerable-instrumentation.patch"
    patch.write_text(
        "diff --git a/src/parser.c b/src/parser.c\n"
        "--- a/src/parser.c\n"
        "+++ b/src/parser.c\n"
        "@@ -1 +1,2 @@\n"
        " old\n"
        "+fprintf(stderr, \"ASSERT_EVT point=root alive=%d false_literal=%d\\n\", 0, 0);\n",
        encoding="utf-8",
    )
    (tmp_path / "field_bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "root.alive": {"expr": "object_is_alive"},
                    "root.false_literal": {"expr": "0"},
                }
            }
        ),
        encoding="utf-8",
    )

    report = repo_workspace._report_template(
        result_dir=tmp_path,
        version="vulnerable",
        target_commit="1111111",
        patch=patch,
        spec={
            "sample_id": "sample",
            "content_hash": "sha256:plan",
            "assertions": [
                {
                    "id": "root.assertion",
                    "kind": "required",
                    "at": "root",
                    "mechanism": "lifetime",
                    "check": ["eq", "$root.alive", "$root.false_literal"],
                    "invariants": ["root.invariant"],
                }
            ],
        },
        apply_proc=subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr=""),
        compile_proc=subprocess.CompletedProcess(args=["build"], returncode=0, stdout="", stderr=""),
        track="repo/secbench",
        setup_masks_failures=False,
    )

    assert report["ok"] is False
    quality = report["check"]["runtime_field_quality"]
    assert quality["valid"] is False
    assert any("$root.alive" in error for error in quality["errors"])
