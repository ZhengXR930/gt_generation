#!/usr/bin/env python3
"""Hydrate, build, validate, and archive non-ARVO runtime workspaces.

This is intentionally conservative and single-worker by default.  A compact GT
package is considered portable only when `runtime_spec.json` can be validated
with artifacts present.  The script first restores any committed archive, then
hydrates source from `sample_info.json`, optionally replays a saved
`reproduction_report.setup_command`, validates the executable, and finally
creates `runtime_work.tar.gz`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "gt_generation"))

from evaluator.reachability.runtime_spec import (  # noqa: E402
    RuntimeSpecError,
    compile_runtime_spec,
)
from gt_toolkit.evidence import write_commitment  # noqa: E402
from gt_toolkit.prepare import (  # noqa: E402
    RUNTIME_ARCHIVE_ROOTS,
    _runtime_spec_root_paths,
    create_runtime_archive,
    hydrate_runtime,
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_non_arvo_ids(gt_root: Path, sample_ids: list[str] | None) -> list[str]:
    if sample_ids:
        return [sample for sample in sample_ids if not sample.startswith("arvo_")]
    manifest = _load_json(gt_root / "valid_gt.json")
    samples = manifest.get("samples")
    if not isinstance(samples, list):
        raise RuntimeError("gt_results/valid_gt.json has no samples list")
    return [str(sample) for sample in samples if not str(sample).startswith("arvo_")]


def _copy_optional_original_report(sample_dir: Path, original_gt_root: Path | None) -> bool:
    if original_gt_root is None or (sample_dir / "reproduction_report.json").is_file():
        return False
    source = original_gt_root / sample_dir.name / "reproduction_report.json"
    if not source.is_file():
        return False
    shutil.copy(source, sample_dir / "reproduction_report.json")
    return True


def _inner_build_command(raw: str) -> tuple[str, bool]:
    raw = raw.strip()
    if not raw:
        return "", False
    try:
        import shlex

        parts = shlex.split(raw)
    except ValueError:
        parts = []
    build_as_root = (
        "/usr/" in raw
        or " apt-get " in f" {raw} "
        or " make install" in f" {raw} "
        or " ldconfig" in f" {raw} "
    )
    for index, item in enumerate(parts):
        if item.endswith("build.sh") and index + 1 < len(parts):
            return parts[index + 1], build_as_root
    for index in range(len(parts) - 2):
        if parts[index:index + 2] == ["bash", "-lc"]:
            return parts[index + 2], build_as_root
    return raw, build_as_root


def _run_build(sample_dir: Path, command: str, build_as_root: bool, timeout: int) -> dict[str, Any]:
    env = os.environ.copy()
    if build_as_root:
        env["GT_BUILD_AS_ROOT"] = "1"
    env.setdefault("GT_BUILD_JOBS", "2")
    limited_command = _limit_build_parallelism(command)
    proc = subprocess.run(
        [str(sample_dir / "build.sh"), limited_command],
        cwd=str(sample_dir),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
        check=False,
    )
    log_dir = sample_dir / "runtime_materialize_logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / "build_stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (log_dir / "build_stderr.txt").write_text(proc.stderr, encoding="utf-8")
    return {
        "returncode": proc.returncode,
        "build_as_root": build_as_root,
        "command": limited_command,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def _limit_build_parallelism(command: str) -> str:
    """Keep one-sample runtime materialization from using every host core."""
    jobs = "${GT_BUILD_JOBS:-2}"
    replacements = {
        '$(nproc)': jobs,
        '"$(nproc)"': f'"{jobs}"',
        "'$(nproc)'": f"'{jobs}'",
        '`nproc`': jobs,
        '$(getconf _NPROCESSORS_ONLN)': jobs,
        '"$(getconf _NPROCESSORS_ONLN)"': f'"{jobs}"',
        "'$(getconf _NPROCESSORS_ONLN)'": f"'{jobs}'",
    }
    limited = command
    for old, new in replacements.items():
        limited = limited.replace(old, new)
    limited = re.sub(r"(?<!\S)-j\s*([0-9]+)", f"-j {jobs}", limited)
    limited = re.sub(r"(?<!\S)-j([0-9]+)", f"-j{jobs}", limited)
    return limited


def _gt_root_paths_in_command(command: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"/gt/(?P<path>[^\s:'\";|&]+)", command):
        path = match.group("path").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _is_build_input_path(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in {
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    }


def _stage_missing_root_build_inputs(sample_dir: Path, command: str) -> list[dict[str, str]]:
    """Restore root-level harness files referenced by older reproduction recipes."""
    staged: list[dict[str, str]] = []
    search_roots = [
        sample_dir / "oss_fuzz_project",
        sample_dir / "harness_downloads",
        sample_dir / "_work" / "src" / "fuzz",
        sample_dir / "_work" / "src",
    ]
    for relative in _gt_root_paths_in_command(command):
        if "/" in relative or not _is_build_input_path(relative):
            continue
        destination = sample_dir / relative
        if destination.exists():
            continue
        candidates = [relative]
        if relative.startswith("repro_"):
            candidates.append(relative[len("repro_"):])
        source = None
        for root in search_roots:
            if not root.exists():
                continue
            for name in candidates:
                direct = root / name
                if direct.is_file():
                    source = direct
                    break
            if source is not None:
                break
            basenames = {Path(name).name for name in candidates}
            matches = sorted(
                path for path in root.rglob("*")
                if path.is_file() and path.name in basenames
            )
            if matches:
                source = matches[0]
                break
        if source is None:
            continue
        shutil.copy2(source, destination)
        staged.append({"path": relative, "source": str(source)})
    return staged


def _candidate_build_commands(sample_dir: Path) -> list[tuple[str, str, bool]]:
    commands: list[tuple[str, str, bool]] = []
    report = sample_dir / "reproduction_report.json"
    if report.is_file():
        data = _load_json(report)
        inner, as_root = _inner_build_command(str(data.get("setup_command") or ""))
        if inner:
            commands.append(("reproduction_report.setup_command", inner, as_root))
    if (sample_dir / "oss_fuzz_setup.sh").is_file() or (sample_dir / "oss_fuzz_build.sh").is_file():
        parts = []
        if (sample_dir / "oss_fuzz_setup.sh").is_file():
            # Source setup so exported environment and retry-safe git wrapper
            # remain active while the official build script runs.
            parts.append("source /gt/oss_fuzz_setup.sh")
        if (sample_dir / "oss_fuzz_build.sh").is_file():
            parts.append("bash /gt/oss_fuzz_build.sh")
        commands.append(("oss_fuzz_staged_recipe", " && ".join(parts), True))
    return commands


def materialize_one(
    sample_dir: Path,
    *,
    original_gt_root: Path | None,
    force: bool,
    build_timeout: int,
) -> dict[str, Any]:
    sample_dir = sample_dir.resolve()
    report: dict[str, Any] = {"sample_id": sample_dir.name, "ok": False}
    report["copied_reproduction_report"] = _copy_optional_original_report(
        sample_dir, original_gt_root
    )
    hydration = hydrate_runtime(sample_dir, force=force)
    report["hydration"] = hydration
    if not hydration.get("prepared"):
        report["error"] = hydration.get("reason") or "hydrate failed"
        return report

    try:
        spec = compile_runtime_spec(sample_dir, require_artifacts=True)
        report["runtime_spec_before_build"] = spec.to_dict()
    except RuntimeSpecError as exc:
        report["prebuild_runtime_error"] = str(exc)
    else:
        archive = create_runtime_archive(sample_dir, force=True)
        report["archive"] = archive
        report["ok"] = bool(archive.get("ok"))
        if report["ok"]:
            write_commitment(sample_dir)
        return report

    build_commands = _candidate_build_commands(sample_dir)
    if not build_commands and hydration.get("reused") and not force:
        # Older compact workspaces can predate OSS-Fuzz recipe staging.  A
        # reused source tree alone is therefore not enough to conclude that
        # the sample has no reproducible build.  Re-run deterministic prepare
        # once so current official project material is staged.
        refreshed_hydration = hydrate_runtime(sample_dir, force=True)
        report["recipe_refresh_hydration"] = refreshed_hydration
        if refreshed_hydration.get("prepared"):
            try:
                spec = compile_runtime_spec(sample_dir, require_artifacts=True)
            except RuntimeSpecError as exc:
                report["post_refresh_runtime_error"] = str(exc)
            else:
                report["runtime_spec_after_refresh"] = spec.to_dict()
                archive = create_runtime_archive(sample_dir, force=True)
                report["archive"] = archive
                report["ok"] = bool(archive.get("ok"))
                if report["ok"]:
                    write_commitment(sample_dir)
                return report
            build_commands = _candidate_build_commands(sample_dir)

    build_attempts = []
    for source, command, as_root in build_commands:
        staged_inputs = _stage_missing_root_build_inputs(sample_dir, command)
        try:
            build_report = _run_build(sample_dir, command, as_root, build_timeout)
        except subprocess.TimeoutExpired as exc:
            build_report = {
                "returncode": 124,
                "build_as_root": as_root,
                "stderr_tail": f"build timed out after {exc.timeout}s",
            }
        build_report["source"] = source
        if staged_inputs:
            build_report["staged_build_inputs"] = staged_inputs
        build_attempts.append(build_report)
        try:
            spec = compile_runtime_spec(sample_dir, require_artifacts=True)
        except RuntimeSpecError as exc:
            build_report["runtime_error"] = str(exc)
            continue
        report["runtime_spec_after_build"] = spec.to_dict()
        archive = create_runtime_archive(sample_dir, force=True)
        report["archive"] = archive
        report["ok"] = bool(archive.get("ok"))
        if report["ok"]:
            write_commitment(sample_dir)
        report["build_attempts"] = build_attempts
        return report

    report["build_attempts"] = build_attempts
    if not build_attempts:
        report["error"] = "no saved build recipe; rerun Stage 01 to recover setup_command"
    else:
        report["error"] = "runtime executable still missing after build attempts"
    return report


def cleanup_generated_scratch(sample_dir: Path, *, keep_runtime: bool = False) -> None:
    """Remove local logs/state that are not part of the portable runtime package."""
    for name in (
        "runtime_materialize_logs",
        "prepare_report.json",
        "sample_state.json",
        "patch.diff",
        ".runtime_work_extracted",
    ):
        path = sample_dir / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() or path.is_symlink():
            path.unlink()
    if not keep_runtime:
        names = set(RUNTIME_ARCHIVE_ROOTS)
        names.update(_runtime_spec_root_paths(sample_dir))
        for name in names:
            if name in {"", ".", "poc"}:
                continue
            path = sample_dir / name
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt-root", type=Path, default=REPO_ROOT / "gt_results")
    parser.add_argument("--sample-id", action="append", dest="sample_ids")
    parser.add_argument("--original-gt-root", type=Path)
    parser.add_argument("--report", type=Path, default=REPO_ROOT / "gt_results" / "runtime_materialization_report.json")
    parser.add_argument("--build-timeout", type=int, default=7200)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-runtime", action="store_true")
    args = parser.parse_args(argv)

    rows = []
    for sample_id in _valid_non_arvo_ids(args.gt_root, args.sample_ids):
        sample_dir = args.gt_root / sample_id
        row = materialize_one(
            sample_dir,
            original_gt_root=args.original_gt_root,
            force=args.force,
            build_timeout=args.build_timeout,
        )
        try:
            cleanup_generated_scratch(sample_dir, keep_runtime=args.keep_runtime)
            row["scratch_cleaned"] = not args.keep_runtime
        except Exception as exc:
            row["scratch_cleanup_error"] = str(exc)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        partial_summary = {
            "total": len(rows),
            "ok": sum(1 for item in rows if item.get("ok")),
            "failed": [item["sample_id"] for item in rows if not item.get("ok")],
            "rows": rows,
        }
        args.report.write_text(
            json.dumps(partial_summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    summary = {
        "total": len(rows),
        "ok": sum(1 for row in rows if row.get("ok")),
        "failed": [row["sample_id"] for row in rows if not row.get("ok")],
        "rows": rows,
    }
    args.report.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
