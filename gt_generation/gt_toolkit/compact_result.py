"""Compact a validated GT package to durable assets and final evidence only."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from .package_audit import audit_package
from .evidence import write_commitment


KEEP_FILES = {
    # Repairing a package later needs the original ARVO image/target metadata.
    "prepare_report.json",
    "sample_info.json",
    "build.sh",
    "poc",
    "default_crash_trace.txt",
    "sanitizer_trace.txt",
    "ground_truth.json",
    "verified_invariants.json",
    "verified_assertions.json",
    "assertion_reward_spec.json",
    "field_bindings.json",
    "event_locations.json",
    "assertion_results.json",
    "perturbation_results.json",
    "reachability_report.json",
    "generation_timing.json",
    "generation_provenance.json",
    "evidence_commitment.json",
    # Small evaluator-private rebuild contract.  The workspace and binaries are
    # still removed; this recipe is required to reconstruct them at evaluation.
    "reproduction_report.json",
    "runtime_spec.json",
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
            if path.is_file() or path.is_symlink():
                path.unlink()

    write_commitment(result_dir)
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
