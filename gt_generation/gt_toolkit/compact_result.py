"""Compact a validated GT package to durable assets and final evidence only."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from .package_audit import audit_package


KEEP_FILES = {
    "sample_info.json",
    "build.sh",
    "poc",
    "patch.diff",
    "default_crash_trace.txt",
    "sanitizer_trace.txt",
    "ground_truth.json",
    "verified_invariants.json",
    "verified_assertions.json",
    "assertion_results.json",
    "perturbation_results.json",
    "reachability_report.json",
    "generation_timing.json",
}


def compact_result(result_dir: Path) -> dict[str, Any]:
    result_dir = result_dir.resolve()
    before = audit_package(result_dir)
    if not before["ok"]:
        return {
            "result_dir": str(result_dir),
            "ok": False,
            "removed": [],
            "errors": ["pre-compaction package audit failed", *before["errors"]],
        }

    # The report already contains resolved checkpoints, observed locations, and
    # assertion-event reachability.  Paths under `artifacts` point only to the
    # generation-time debugger/spec/trace inputs that are removed below.
    reachability_path = result_dir / "reachability_report.json"
    reachability = json.loads(reachability_path.read_text(encoding="utf-8"))
    reachability.pop("artifacts", None)
    reachability_path.write_text(
        json.dumps(reachability, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    removed: list[str] = []
    for path in sorted(result_dir.iterdir(), key=lambda item: item.name):
        if path.name in KEEP_FILES:
            continue
        removed.append(path.name)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    after = audit_package(result_dir)
    return {
        "result_dir": str(result_dir),
        "sample_id": after.get("sample_id"),
        "ok": after["ok"],
        "removed": removed,
        "kept": sorted(path.name for path in result_dir.iterdir()),
        "errors": after["errors"],
        "warnings": after["warnings"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    report = compact_result(args.result_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
