#!/usr/bin/env python3
"""Keep benchmark-backed base samples and qualified new memory-PoC samples.

ARVO samples are retained under the ARVO image/dataset PoC guarantee.
SEC-bench samples must have a nonempty local non-HTML PoC.
new_diverse samples must occur in selected_new_memory_poc.json.
Removed records are preserved in a recoverable JSON report.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPO_ROOT / "dataset"
SELECTION = DATASET / "selected_1000.json"
QUALIFIED_NEW = DATASET / "selected_new_memory_poc.json"
POC_AUDIT = DATASET / "new_vulnerabilities_with_poc.json"
REMOVED_REPORT = DATASET / "removed_samples_without_qualified_poc.json"
SUMMARY = DATASET / "selection_summary.json"


def resolve_asset(raw: object) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    candidates = [path] if path.is_absolute() else [
        REPO_ROOT / path,
        DATASET / path,
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def is_real_poc(path: Path | None) -> bool:
    if path is None:
        return False
    raw = path.read_bytes()
    prefix = raw[:2048].lstrip().lower()
    return bool(raw) and not (
        prefix.startswith(b"<!doctype html")
        or prefix.startswith(b"<html")
        or b"<title>sign in" in prefix
    )


def local_poc(sample: dict) -> Path | None:
    return resolve_asset(sample.get("poc_artifact_path")) or resolve_asset(
        sample.get("poc_path")
    )


def removal_reasons(audit: dict) -> dict[str, dict]:
    reasons = {
        record["sample_id"]: {
            key: value for key, value in record.items() if key != "sample_id"
        }
        for record in audit.get("rejected", [])
    }
    for record in audit.get("qualified", []):
        for sample_id in record.get("collapsed_report_ids", []):
            reasons[sample_id] = {
                "reason": "duplicate_vulnerability_signature",
                "retained_representative": record["sample_id"],
                "uniqueness_key": record.get("uniqueness_key"),
            }
    return reasons


def build_summary(samples: list[dict]) -> dict:
    base = [sample for sample in samples if sample.get("selection_group") == "base"]
    new = [
        sample for sample in samples
        if sample.get("selection_group") == "new_diverse"
    ]
    counter = lambda key, values=samples: dict(  # noqa: E731
        sorted(collections.Counter(value.get(key) for value in values).items())
    )
    return {
        "sample_count": len(samples),
        "base_count": len(base),
        "new_diverse_count": len(new),
        "base_source_counts": counter("source_family", base),
        "new_diverse_source_counts": counter("source_dataset", new),
        "source_family_counts": counter("source_family"),
        "source_dataset_counts": counter("source_dataset"),
        "poc_status_counts": counter("poc_status"),
        "poc_runnable_count": sum(bool(sample.get("poc_runnable")) for sample in samples),
        "language_counts": counter("language"),
        "vulnerability_class_counts": counter("vulnerability_class"),
        "unique_project_count": len({sample.get("project") for sample in samples}),
        "qualification": {
            "all_sample_ids_unique": len({sample["sample_id"] for sample in samples})
            == len(samples),
            "arvo_samples_use_dataset_poc_guarantee": all(
                sample.get("poc_status") == "arvo_dataset_guarantee"
                for sample in samples
                if sample.get("source_family") == "arvo"
            ),
            "all_secbench_pocs_local_nonempty_non_html": all(
                is_real_poc(local_poc(sample))
                for sample in samples
                if sample.get("source_family") == "secbench"
            ),
            "all_new_diverse_have_qualified_local_memory_poc": True,
        },
    }


def main() -> int:
    samples = json.loads(SELECTION.read_text(encoding="utf-8"))
    qualified = json.loads(QUALIFIED_NEW.read_text(encoding="utf-8"))
    audit = json.loads(POC_AUDIT.read_text(encoding="utf-8"))
    qualified_ids = {sample["sample_id"] for sample in qualified}
    reasons = removal_reasons(audit)

    kept: list[dict] = []
    removed: list[dict] = []
    for sample in samples:
        if sample.get("selection_group") != "new_diverse":
            kept.append(sample)
            continue
        if sample["sample_id"] in qualified_ids:
            kept.append(sample)
            continue
        detail = reasons.get(
            sample["sample_id"],
            {"reason": "no_real_local_poc_or_web_snapshot"},
        )
        removed.append({"sample": sample, **detail})

    # Deterministic final gates before replacing the selection.
    if len(qualified_ids) != len(qualified):
        raise SystemExit("qualified new selection contains duplicate sample IDs")
    kept_ids = [sample["sample_id"] for sample in kept]
    if len(set(kept_ids)) != len(kept_ids):
        raise SystemExit("cleaned selection would contain duplicate sample IDs")
    kept_new_ids = {
        sample["sample_id"]
        for sample in kept
        if sample.get("selection_group") == "new_diverse"
    }
    if kept_new_ids != qualified_ids:
        raise SystemExit("cleaned new_diverse IDs do not match qualified selection")
    for sample in kept:
        family = sample.get("source_family")
        if family == "arvo":
            if sample.get("poc_status") != "arvo_dataset_guarantee":
                raise SystemExit(f"ARVO PoC guarantee missing: {sample['sample_id']}")
        elif family == "secbench":
            if not is_real_poc(local_poc(sample)):
                raise SystemExit(f"SEC-bench real PoC missing: {sample['sample_id']}")
        elif sample.get("selection_group") == "new_diverse":
            if not is_real_poc(local_poc(sample)):
                raise SystemExit(f"qualified new real PoC missing: {sample['sample_id']}")

    report = {
        "policy": {
            "arvo": "retain only records with ARVO dataset/image PoC guarantee",
            "secbench": "require nonempty local non-HTML PoC",
            "new_diverse": "require membership in selected_new_memory_poc.json",
        },
        "before_count": len(samples),
        "after_count": len(kept),
        "removed_count": len(removed),
        "retained_base_count": sum(
            sample.get("selection_group") == "base" for sample in kept
        ),
        "retained_qualified_new_count": len(qualified_ids),
        "removed_reason_counts": dict(
            sorted(collections.Counter(item["reason"] for item in removed).items())
        ),
        "removed": removed,
    }
    REMOVED_REPORT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    SELECTION.write_text(
        json.dumps(kept, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    SUMMARY.write_text(
        json.dumps(build_summary(kept), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "removed"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
