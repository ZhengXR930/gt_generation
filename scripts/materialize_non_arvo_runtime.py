#!/usr/bin/env python3
"""Hydrate, build, and optionally archive non-ARVO runtime workspaces.

This is intentionally conservative and single-worker by default.  Stable
repo-track packages should be portable through `runtime_spec.json` plus the
small `runtime_build.json` recipe, so this script validates that the target can
be rebuilt in `gt-memory-env` without committing the whole `_work` tree.
Use `--package-archive` only for samples that cannot be rebuilt reliably.
"""

from __future__ import annotations

import argparse
import json
import shutil
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
    build_runtime_artifacts,
    create_runtime_archive,
    write_runtime_build_recipe,
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


def materialize_one(
    sample_dir: Path,
    *,
    original_gt_root: Path | None,
    force: bool,
    build_timeout: int,
    package_archive: bool,
) -> dict[str, Any]:
    sample_dir = sample_dir.resolve()
    report: dict[str, Any] = {"sample_id": sample_dir.name, "ok": False}
    report["copied_reproduction_report"] = _copy_optional_original_report(
        sample_dir, original_gt_root
    )
    try:
        spec = compile_runtime_spec(sample_dir, require_artifacts=True)
        report["runtime_spec_before_build"] = spec.to_dict()
    except RuntimeSpecError as exc:
        report["prebuild_runtime_error"] = str(exc)
    else:
        recipe = write_runtime_build_recipe(sample_dir)
        report["runtime_build"] = recipe
        if package_archive:
            archive = create_runtime_archive(sample_dir, force=True)
            report["archive"] = archive
            report["ok"] = bool(archive.get("ok"))
        else:
            report["ok"] = bool(recipe.get("written"))
            if not report["ok"]:
                report["error"] = (
                    recipe.get("reason") or "runtime executable exists but no rebuild recipe was written"
                )
        if report["ok"] and package_archive:
            write_commitment(sample_dir)
        return report

    build = build_runtime_artifacts(
        sample_dir,
        force_hydrate=force,
        timeout=build_timeout,
    )
    report["runtime_build_report"] = build
    if build.get("built"):
        try:
            spec = compile_runtime_spec(sample_dir, require_artifacts=True)
        except RuntimeSpecError as exc:
            report["postbuild_runtime_error"] = str(exc)
            report["error"] = "runtime build command succeeded but executable validation failed"
            return report
        report["runtime_spec_after_build"] = spec.to_dict()
        recipe = write_runtime_build_recipe(sample_dir)
        report["runtime_build"] = recipe
        report["ok"] = bool(recipe.get("written"))
        if not report["ok"]:
            report["error"] = (
                recipe.get("reason") or "runtime executable built but no rebuild recipe was written"
            )
            return report
        if package_archive:
            archive = create_runtime_archive(sample_dir, force=True)
            report["archive"] = archive
            report["ok"] = bool(archive.get("ok"))
        if report["ok"] and package_archive:
            write_commitment(sample_dir)
        return report

    report["error"] = build.get("reason") or "runtime executable still missing after build attempts"
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
    parser.add_argument(
        "--package-archive",
        action="store_true",
        help="Also create runtime_work.tar.gz for samples that cannot be rebuilt elsewhere.",
    )
    args = parser.parse_args(argv)

    rows = []
    for sample_id in _valid_non_arvo_ids(args.gt_root, args.sample_ids):
        sample_dir = args.gt_root / sample_id
        row = materialize_one(
            sample_dir,
            original_gt_root=args.original_gt_root,
            force=args.force,
            build_timeout=args.build_timeout,
            package_archive=args.package_archive,
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
