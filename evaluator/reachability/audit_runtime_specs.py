#!/usr/bin/env python3
"""Audit whether every saved model result has a scorable reachability runtime."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from reachability.engine import extract_reachability_checkpoints
from reachability.runtime_spec import RuntimeSpecError, compile_runtime_spec


REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"
REQUIRED_KINDS = {"parser_admitted", "source", "root_cause_line", "sink_line"}


def audit(models: set[str]) -> dict[str, Any]:
    rows = []
    for model_dir in sorted(POC_RESULTS.iterdir()):
        if not model_dir.is_dir() or model_dir.name.startswith("_"):
            continue
        if models and model_dir.name not in models:
            continue
        for manifest_path in sorted(model_dir.glob("*/manifest.json")):
            rows.append(_audit_one(model_dir.name, manifest_path))
    counts = Counter(row["reachability_status"] for row in rows)
    with_poc = [row for row in rows if row["submitted_unique_pocs"] > 0]
    return {
        "protocol": "runtime-spec-readiness-v1",
        "rows": rows,
        "summary": {
            "samples": len(rows),
            "samples_with_poc": len(with_poc),
            "submitted_unique_pocs": sum(
                row["submitted_unique_pocs"] for row in rows
            ),
            "status_counts": dict(sorted(counts.items())),
            "with_poc_runtime_capable": sum(
                row["reachability_status"] in {
                    "runtime_ready",
                    "runtime_image_pull_required",
                    "runtime_rebuild_required",
                }
                for row in with_poc
            ),
            "with_poc_not_ready": sum(
                row["reachability_status"] not in {
                    "runtime_ready",
                    "runtime_image_pull_required",
                    "runtime_rebuild_required",
                }
                for row in with_poc
            ),
        },
    }


def _audit_one(model: str, manifest_path: Path) -> dict[str, Any]:
    sample_dir = manifest_path.parent
    sample_id = sample_dir.name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidates = manifest.get("deduplicated_pocs")
    submitted = len(candidates) if isinstance(candidates, list) else int(
        (manifest.get("poc_deduplication") or {}).get("deduplicated_poc_count") or 0
    )
    row: dict[str, Any] = {
        "model": model,
        "sample_id": sample_id,
        "submitted_unique_pocs": submitted,
    }
    if submitted == 0:
        row["reachability_status"] = "not_applicable_no_poc"
        return row
    gt_dir = GT_RESULTS / sample_id
    if not (gt_dir / "verified_assertions.json").is_file():
        row["reachability_status"] = "excluded_no_strict_gt"
        return row
    missing_candidates = []
    for candidate in candidates or []:
        relative = str(candidate.get("representative_poc_path") or "")
        if not relative or not (sample_dir / relative).is_file():
            missing_candidates.append(str(candidate.get("representative_attempt_id") or ""))
    if missing_candidates:
        row.update({
            "reachability_status": "candidate_artifact_missing",
            "missing_candidate_attempts": missing_candidates,
        })
        return row
    try:
        spec = compile_runtime_spec(gt_dir, require_artifacts=False)
    except RuntimeSpecError as exc:
        row.update({
            "reachability_status": "runtime_spec_unavailable",
            "runtime_error": str(exc),
        })
        return row
    gt = json.loads((gt_dir / "ground_truth.json").read_text(encoding="utf-8"))
    kinds = {item.get("kind") for item in extract_reachability_checkpoints(gt)}
    missing_kinds = sorted(REQUIRED_KINDS - kinds)
    if missing_kinds:
        row.update({
            "reachability_status": "checkpoint_contract_incomplete",
            "missing_checkpoint_kinds": missing_kinds,
            "runtime_spec": spec.to_dict(),
        })
        return row
    image_local = subprocess.run(
        ["docker", "image", "inspect", spec.image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    local_artifacts_ready = (
        spec.backend != "local_workspace"
        or (gt_dir / "_work" / "src").is_dir()
    )
    status = (
        "runtime_rebuild_required"
        if not local_artifacts_ready
        else (
            "runtime_ready"
            if image_local
            else "runtime_image_pull_required"
        )
    )
    row.update({
        "reachability_status": status,
        "runtime_backend": spec.backend,
        "runtime_image": spec.image,
        "runtime_image_local": image_local,
    })
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = audit(set(args.model or []))
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    return 1 if report["summary"]["with_poc_not_ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
