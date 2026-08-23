"""Bind a GT narrative to the exact evidence bundle that verifies it."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .prepare import runtime_archive_artifact_names
except ImportError:  # pragma: no cover - direct script compatibility
    from gt_toolkit.prepare import runtime_archive_artifact_names


COMMITMENT_FILES = (
    "ground_truth.json",
    "verified_assertions.json",
    "verified_invariants.json",
    "assertion_results.json",
    "perturbation_results.json",
    "field_bindings.json",
    "event_locations.json",
    "reachability_report.json",
)

OPTIONAL_COMMITMENT_FILES = (
    "assertion_reward_spec.json",
    "context_trace.json",
    "runtime_build.json",
    "runtime_materials.json",
    "portability_report.json",
    "runtime_work.tar.gz",
    "runtime_work.tgz",
    "runtime_work.tar.xz",
    "runtime_work.tar.bz2",
    "runtime_work.tar",
    "runtime_work_manifest.json",
)


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def build_commitment(result_dir: Path) -> dict[str, Any]:
    result_dir = result_dir.resolve()
    missing = [name for name in COMMITMENT_FILES if not (result_dir / name).is_file()]
    if missing:
        raise ValueError("cannot bind incomplete evidence bundle: " + ", ".join(missing))
    ground_truth = json.loads(
        (result_dir / "ground_truth.json").read_text(encoding="utf-8")
    )
    optional_names = [
        optional
        for optional in OPTIONAL_COMMITMENT_FILES
        if (result_dir / optional).is_file()
    ]
    optional_names.extend(runtime_archive_artifact_names(result_dir))
    optional_names = sorted(dict.fromkeys(optional_names))
    return {
        "schema_version": "gt-evidence-commitment-v1",
        "sample_id": str(ground_truth.get("sample_id") or ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {
            name: file_sha256(result_dir / name)
            for name in (
                *COMMITMENT_FILES,
                *optional_names,
            )
        },
    }


def write_commitment(result_dir: Path) -> dict[str, Any]:
    commitment = build_commitment(result_dir)
    path = result_dir / "evidence_commitment.json"
    path.write_text(
        json.dumps(commitment, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return commitment


def commitment_errors(result_dir: Path, commitment: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if commitment.get("schema_version") != "gt-evidence-commitment-v1":
        errors.append("evidence_commitment.json has an unsupported schema_version")
    files = commitment.get("files")
    if not isinstance(files, dict):
        return [*errors, "evidence_commitment.json files must be an object"]
    for name in COMMITMENT_FILES:
        path = result_dir / name
        declared = files.get(name)
        if not path.is_file():
            errors.append(f"committed evidence file is missing: {name}")
        elif declared != file_sha256(path):
            errors.append(f"committed evidence hash does not match {name}")
    optional_names = set(OPTIONAL_COMMITMENT_FILES)
    optional_names.update(runtime_archive_artifact_names(result_dir))
    for name in sorted(optional_names):
        if name not in files:
            continue
        path = result_dir / name
        declared = files.get(name)
        if not path.is_file():
            continue
        if declared != file_sha256(path):
            errors.append(f"committed evidence hash does not match {name}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        commitment = write_commitment(args.result_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(commitment, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
