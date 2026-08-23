"""Deterministic Stage 01 portability gate for non-ARVO samples."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any

from .prepare import (
    _inner_build_command,
    _run_runtime_build,
    hydrate_repo_source,
    write_runtime_build_recipe,
)
from .repo_workspace import command_masks_failures


PORTABILITY_REPORT_NAME = "portability_report.json"
RUNTIME_MATERIALS_NAME = "runtime_materials.json"
BASE_PORTABLE_FILES = (
    "sample_info.json",
    "build.sh",
    "poc",
    "runtime_build.json",
    "runtime_spec.json",
)
OPTIONAL_MATERIALS = {
    "oss_fuzz_build.sh": ("oss_fuzz_build.sh", "oss_fuzz_project"),
    "oss_fuzz_setup.sh": (
        "oss_fuzz_setup.sh",
        "oss_fuzz_project",
        "oss_fuzz_src",
    ),
    "harness_downloads": ("harness_downloads",),
}
SANITIZER_MARKERS = (
    "AddressSanitizer",
    "MemorySanitizer",
    "UndefinedBehaviorSanitizer",
    "LeakSanitizer",
    "ThreadSanitizer",
    "runtime error:",
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_spec_module():
    try:
        from evaluator.reachability import runtime_spec
    except ImportError:
        from reachability import runtime_spec
    return runtime_spec


def freeze_runtime_contract(result_dir: str | Path) -> dict[str, Any]:
    """Freeze build/run contracts after the Stage 01 agent has succeeded."""
    result_path = Path(result_dir).resolve()
    if result_path.name.startswith("arvo_"):
        return {"ok": True, "skipped": "ARVO uses immutable images"}
    required = (
        "sample_info.json", "build.sh", "poc", "reproduction_report.json"
    )
    missing = [name for name in required if not (result_path / name).is_file()]
    if missing:
        return {
            "ok": False,
            "reason": "missing Stage 01 inputs: " + ", ".join(missing),
        }
    reproduction = _load(result_path / "reproduction_report.json")
    if reproduction.get("vulnerable_reproduced") is not True:
        return {"ok": False, "reason": "vulnerable reproduction was not established"}
    if reproduction.get("matches_issue") is not True:
        return {"ok": False, "reason": "vulnerable finding does not match the issue"}
    setup, run_as_root = _inner_build_command(
        str(reproduction.get("setup_command") or "")
    )
    run_as_root = run_as_root or "GT_BUILD_AS_ROOT=1" in str(
        reproduction.get("setup_command") or ""
    )
    if not setup:
        return {"ok": False, "reason": "reproduction_report.setup_command is empty"}
    if command_masks_failures(setup):
        return {"ok": False, "reason": "setup_command masks build failures"}
    recipe = write_runtime_build_recipe(
        result_path,
        [{
            "source": "reproduction_report.setup_command",
            "command": setup,
            "run_as_root": run_as_root,
            "environment": {"GT_BUILD_JOBS": "1"},
        }],
    )
    if not recipe.get("written"):
        return {
            "ok": False,
            "reason": str(recipe.get("reason") or "build recipe unavailable"),
        }
    try:
        runtime_spec = _runtime_spec_module()
        spec = runtime_spec.compile_runtime_spec(
            result_path, require_artifacts=False, prefer_frozen=False
        )
        sample = _load(result_path / "sample_info.json")
        portable_setup = _command_for_version(setup, sample, "vulnerable")
        spec_data = spec.to_dict()
        spec_data.update({
            # The evaluator restores source_commit itself.  Keep agent-authored
            # checkout/reset/clean commands out of the published build entry.
            "build_commands": [portable_setup],
            "build_workdir": "/gt/_work/src",
            "source_repo": str(
                sample.get("repo") or sample.get("repo_url") or ""
            ),
            "source_commit": _target_commit(sample, "vulnerable"),
        })
        (result_path / "runtime_spec.json").write_text(
            json.dumps(spec_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"cannot freeze runtime_spec.json: {exc}",
        }
    manifest = write_runtime_materials_manifest(result_path)
    manifest_path = result_path / RUNTIME_MATERIALS_NAME
    manifest_summary = {
        "path": RUNTIME_MATERIALS_NAME,
        "file_count": len(manifest["files"]),
        "sha256": _sha256(manifest_path),
    }
    material_errors = _validate_recipe_material_references(result_path)
    if material_errors:
        return {
            "ok": False,
            "reason": "; ".join(material_errors),
            "recipe_command_count": recipe.get("commands", 0),
            "manifest": manifest_summary,
        }
    return {
        "ok": True,
        "recipe_command_count": recipe.get("commands", 0),
        "manifest": manifest_summary,
    }


def _root_materials_from_recipe(result_path: Path) -> list[Path]:
    recipe = _load(result_path / "runtime_build.json")
    paths: list[Path] = []
    for item in recipe.get("commands") or []:
        command = str(item.get("command") or "") if isinstance(item, dict) else ""
        for match in re.finditer(r"/gt/(?P<path>[^\s:'\";|&]+)", command):
            relative = match.group("path").strip().rstrip(")")
            top = relative.split("/", 1)[0]
            if top in {"", ".", "..", "poc", "_work", "_out"}:
                continue
            path = result_path / top
            if path.is_file() and path not in paths:
                paths.append(path)
            elif path.is_dir() and not path.is_symlink() and path not in paths:
                paths.append(path)
    return paths


def _validate_recipe_material_references(result_path: Path) -> list[str]:
    """Reject host-only or missing result-root inputs before replay starts."""
    errors: list[str] = []
    recipe = _load(result_path / "runtime_build.json")
    result_text = str(result_path)
    for item in recipe.get("commands") or []:
        command = str(item.get("command") or "") if isinstance(item, dict) else ""
        if result_text in command:
            errors.append("runtime build command contains the generator host result path")
        for match in re.finditer(r"/gt/(?P<path>[^\s:'\";|&]+)", command):
            relative = match.group("path").strip().rstrip(")")
            if relative.startswith(("_work", "_out", "poc")):
                continue
            top = relative.split("/", 1)[0]
            if top in {"", ".", ".."}:
                errors.append(
                    f"unsafe result-root path referenced by runtime build: /gt/{relative}"
                )
                continue
            if top in {"build.sh", "runtime_build.json", "runtime_spec.json"}:
                continue
            # Shell redirection targets and ordinary logs are generated outputs,
            # not portable build inputs.
            before = command[max(0, match.start() - 4):match.start()]
            if ">" in before or top.endswith((".log", ".txt")):
                continue
            material = result_path / top
            if material.is_symlink() or not material.exists():
                errors.append(
                    f"missing publishable build material referenced as /gt/{top}"
                )
    return sorted(set(errors))


def portable_material_paths(result_dir: str | Path) -> list[Path]:
    result_path = Path(result_dir).resolve()
    paths = [result_path / name for name in BASE_PORTABLE_FILES]
    if (result_path / "runtime_build.json").is_file():
        recipe_text = (result_path / "runtime_build.json").read_text(
            encoding="utf-8", errors="replace"
        )
        for marker, names in OPTIONAL_MATERIALS.items():
            if marker in recipe_text:
                paths.extend(result_path / name for name in names)
        paths.extend(_root_materials_from_recipe(result_path))
    unique: list[Path] = []
    for path in paths:
        if path.exists() and path not in unique:
            unique.append(path)
    return unique


def write_runtime_materials_manifest(result_dir: str | Path) -> dict[str, Any]:
    result_path = Path(result_dir).resolve()
    entries: list[dict[str, Any]] = []
    for root in portable_material_paths(result_path):
        files = (
            [root]
            if root.is_file()
            else sorted(path for path in root.rglob("*") if path.is_file())
        )
        for path in files:
            relative = path.relative_to(result_path)
            if ".git" in relative.parts:
                continue
            entries.append({
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            })
    manifest = {
        "schema_version": "gt-runtime-materials-v1",
        "sample_id": result_path.name,
        "files": entries,
    }
    (result_path / RUNTIME_MATERIALS_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def _copy_portable_materials(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in portable_material_paths(source):
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(
                path,
                target,
                symlinks=False,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(path, target)


def _target_commit(sample: dict[str, Any], version: str) -> str:
    if version == "fixed":
        return str(
            sample.get("fix_commit") or sample.get("fixed_commit") or ""
        ).strip()
    return str(
        sample.get("vulnerable_commit") or sample.get("vul_commit") or ""
    ).strip()


def _command_for_version(
    command: str, sample: dict[str, Any], version: str
) -> str:
    vulnerable = _target_commit(sample, "vulnerable")
    target = _target_commit(sample, version)
    if version == "fixed" and vulnerable and target:
        command = command.replace(vulnerable, target)
    # Source selection belongs to the deterministic gate.  Leaving checkout or
    # reset commands in an agent-authored build recipe can switch the fixed
    # replay back to vulnerable or make the recipe depend on current state.
    lines = []
    for line in command.splitlines():
        if re.match(
            r"^\s*git\s+(?:checkout|reset\s+--hard|clean\s+-)[^\n]*$",
            line,
        ):
            continue
        lines.append(line)
    return "\n".join(lines)


def _checkout(result_path: Path, commit: str) -> dict[str, Any]:
    src = result_path / "_work" / "src"
    proc = subprocess.run(
        ["git", "-C", str(src), "checkout", "-f", commit],
        text=True,
        capture_output=True,
        errors="replace",
        check=False,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stderr_tail": proc.stderr[-2000:],
    }


def _artifact_path(result_path: Path, spec: Any) -> Path | None:
    try:
        return _runtime_spec_module().container_path_on_host(
            result_path, spec.executable, spec.workdir
        )
    except Exception:
        return None


def _build_side(result_path: Path, version: str, timeout: int) -> dict[str, Any]:
    sample = _load(result_path / "sample_info.json")
    commit = _target_commit(sample, version)
    hydration = hydrate_repo_source(result_path)
    if not hydration.get("prepared"):
        return {
            "ok": False,
            "commit": commit,
            "reason": str(hydration.get("reason") or "source hydration failed"),
        }
    checkout = _checkout(result_path, commit)
    if not checkout["ok"]:
        return {
            "ok": False,
            "commit": commit,
            "checkout": checkout,
        }
    spec = _runtime_spec_module().compile_runtime_spec(
        result_path, require_artifacts=False, prefer_frozen=True
    )
    recipe = _load(result_path / "runtime_build.json")
    attempts: list[dict[str, Any]] = []
    for item in recipe.get("commands") or []:
        if not isinstance(item, dict):
            continue
        command = _command_for_version(
            str(item.get("command") or ""), sample, version
        )
        if not command or command_masks_failures(command):
            continue
        environment = dict(item.get("environment") or {})
        environment["GT_REPO_ROOT"] = str(REPO_ROOT)
        attempt = _run_runtime_build(
            result_path,
            command,
            build_as_root=bool(item.get("run_as_root")),
            extra_env=environment,
            timeout=timeout,
        )
        attempts.append(_summarize_build_attempt(attempt))
        artifact = _artifact_path(result_path, spec)
        if (
            attempt.get("returncode") == 0
            and artifact is not None
            and artifact.is_file()
            and artifact.stat().st_mode & 0o111
        ):
            return {
                "ok": True,
                "commit": commit,
                "checkout": checkout,
                "selected_command": command,
                "artifact": str(artifact.relative_to(result_path)),
                "attempts": attempts,
            }
    return {
        "ok": False,
        "commit": commit,
        "checkout": checkout,
        "attempts": attempts,
        "reason": "no build command produced the runtime_spec executable",
    }


def _summarize_build_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "returncode": attempt.get("returncode"),
        "build_as_root": attempt.get("build_as_root") is True,
        "failure_markers": attempt.get("failure_markers") or [],
    }
    if attempt.get("returncode") != 0:
        summary["stdout_tail"] = str(attempt.get("stdout_tail") or "")[-2000:]
        summary["stderr_tail"] = str(attempt.get("stderr_tail") or "")[-2000:]
    return summary


def _run_frozen_spec(result_path: Path, timeout: int) -> dict[str, Any]:
    spec = _runtime_spec_module().compile_runtime_spec(
        result_path, require_artifacts=True, prefer_frozen=True
    )
    env_prefix = " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in sorted(spec.environment.items())
    )
    argv = [spec.executable] + [
        item.replace(spec.input_placeholder, "/gt/poc")
        for item in spec.arguments
    ]
    invocation = " ".join(shlex.quote(item) for item in argv)
    command = (
        f"cd {shlex.quote(spec.workdir)} && "
        + (env_prefix + " " if env_prefix else "")
        + invocation
    )
    try:
        environment = dict(os.environ)
        environment["GT_REPO_ROOT"] = str(REPO_ROOT)
        proc = subprocess.run(
            [str(result_path / "build.sh"), command],
            cwd=result_path,
            text=True,
            capture_output=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env=environment,
        )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return {
            "returncode": proc.returncode,
            "command": command,
            "execution_valid": 0 <= proc.returncode < 128,
            "finding_present": _finding_present(output),
            "finding_signature": _finding_signature(output),
            **(
                {
                    "stdout_tail": (proc.stdout or "")[-4000:],
                    "stderr_tail": (proc.stderr or "")[-4000:],
                }
                if proc.returncode in {124, 126, 127} or proc.returncode < 0
                else {}
            ),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": 124,
            "command": command,
            "execution_valid": False,
            "finding_present": False,
            "stderr_tail": f"runtime execution timed out after {exc.timeout}s",
        }


def _finding_present(text: str) -> bool:
    return any(marker in text for marker in SANITIZER_MARKERS)


def _finding_signature(text: str) -> dict[str, Any]:
    try:
        from evaluator.reachability.engine import parse_sanitizer_trace
    except ImportError:
        from reachability.engine import parse_sanitizer_trace
    return parse_sanitizer_trace(text)


def _finding_signature_matches(
    expected: dict[str, Any], observed: dict[str, Any]
) -> bool:
    if not observed or not (
        observed.get("sanitizer") or observed.get("crash_type")
    ):
        return False
    expected_sanitizer = str(expected.get("sanitizer") or "").lower()
    observed_sanitizer = str(observed.get("sanitizer") or "").lower()
    if expected_sanitizer and observed_sanitizer != expected_sanitizer:
        return False
    expected_type = str(expected.get("crash_type") or "").lower()
    observed_type = str(observed.get("crash_type") or "").lower()
    if expected_type and observed_type and expected_type != observed_type:
        return False
    expected_location = expected.get("crash_location") or {}
    observed_location = observed.get("crash_location") or {}
    expected_file = Path(str(expected_location.get("file") or "")).name
    observed_file = Path(str(observed_location.get("file") or "")).name
    if expected_file and observed_file and expected_file != observed_file:
        return False
    expected_line = expected_location.get("line")
    observed_line = observed_location.get("line")
    if expected_line and observed_line and int(expected_line) != int(observed_line):
        return False
    return True


def _same_finding(
    baseline: str, replay_signature: dict[str, Any]
) -> bool:
    if not _finding_present(baseline) or not replay_signature:
        return False
    expected = _finding_signature(baseline)
    return _finding_signature_matches(expected, replay_signature)


def run_portability_gate(
    result_dir: str | Path, *, timeout: int = 7200
) -> dict[str, Any]:
    """Run clean vulnerable/fixed replay and persist only its small report."""
    source = Path(result_dir).resolve()
    report: dict[str, Any] = {
        "schema_version": "gt-stage01-portability-v1",
        "sample_id": source.name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "vulnerable_build_ok": False,
        "vulnerable_triggered": False,
        "fixed_build_ok": False,
        "fixed_not_triggered": False,
        "clean_replay_ok": False,
        "runtime_portable": False,
    }
    try:
        frozen = freeze_runtime_contract(source)
        report["contract"] = frozen
        if not frozen.get("ok"):
            report["reason"] = frozen.get("reason") or "contract freeze failed"
            return _write_report(source, report)
        if source.name.startswith("arvo_"):
            report.update({
                "clean_replay_ok": True,
                "runtime_portable": True,
                "skipped": "ARVO image protocol",
            })
            return _write_report(source, report)
        baseline = (source / "sanitizer_trace.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        if not _finding_present(baseline):
            report["reason"] = "Stage 01 sanitizer_trace has no sanitizer finding"
            return _write_report(source, report)
        baseline_signature = _finding_signature(baseline)
        with tempfile.TemporaryDirectory(
            prefix=f"gt-portability-{source.name}-"
        ) as temporary:
            replay = Path(temporary) / source.name
            _copy_portable_materials(source, replay)
            vulnerable = _build_side(replay, "vulnerable", timeout)
            report["vulnerable"] = vulnerable
            report["vulnerable_build_ok"] = vulnerable.get("ok") is True
            if report["vulnerable_build_ok"]:
                run = _run_frozen_spec(replay, timeout)
                vulnerable["run"] = run
                report["vulnerable_triggered"] = _same_finding(
                    baseline, run.get("finding_signature") or {}
                )

            shutil.rmtree(replay, ignore_errors=True)
            _copy_portable_materials(source, replay)
            fixed = _build_side(replay, "fixed", timeout)
            report["fixed"] = fixed
            report["fixed_build_ok"] = fixed.get("ok") is True
            if report["fixed_build_ok"]:
                run = _run_frozen_spec(replay, timeout)
                fixed["run"] = run
                fixed_signature = run.get("finding_signature") or {}
                report["fixed_not_triggered"] = (
                    run.get("execution_valid") is True
                    and not _finding_signature_matches(
                        baseline_signature, fixed_signature
                    )
                )
        report["clean_replay_ok"] = all(
            report[key]
            for key in (
                "vulnerable_build_ok",
                "vulnerable_triggered",
                "fixed_build_ok",
                "fixed_not_triggered",
            )
        )
        report["runtime_portable"] = report["clean_replay_ok"]
        if not report["runtime_portable"]:
            report["reason"] = (
                "clean vulnerable/fixed replay did not satisfy portability oracle"
            )
    except Exception as exc:
        report["reason"] = (
            f"portability replay failed: {type(exc).__name__}: {exc}"
        )
    return _write_report(source, report)


def _write_report(result_path: Path, report: dict[str, Any]) -> dict[str, Any]:
    (result_path / PORTABILITY_REPORT_NAME).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def portability_gate_passes(result_dir: str | Path) -> bool:
    result_path = Path(result_dir)
    if result_path.name.startswith("arvo_"):
        return True
    try:
        report = _load(result_path / PORTABILITY_REPORT_NAME)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        report.get("runtime_portable") is True
        and report.get("clean_replay_ok") is True
    )


def materialize_stage01_portability(
    result_dir: str | Path, *, timeout: int = 7200
) -> dict[str, Any]:
    """Run the gate, then remove generation-only runtime state on success."""
    result_path = Path(result_dir).resolve()
    report = run_portability_gate(result_path, timeout=timeout)
    if report.get("runtime_portable") is not True:
        return report
    for name in ("_work", "_out", "runtime_build_logs"):
        path = result_path / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args(argv)
    report = run_portability_gate(args.result_dir, timeout=args.timeout)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("runtime_portable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
