#!/usr/bin/env python3
"""Backfill per-sample and aggregate PoC deduplication metadata."""

from __future__ import annotations

import json
from pathlib import Path

from poc_dedup import deduplicate_submission_attempts


HERE = Path(__file__).resolve().parent
RESULTS_ROOT = HERE.parent / "poc_results"
MODEL_NAMESPACES = ("deepseek-v4-flash", "gpt-5.4-mini")


def main() -> None:
    aggregate = {
        "deduplication_key": "(model_namespace, sample_id, poc_sha256)",
        "representative_policy": "last_submission_trace",
        "models": {},
    }
    overall_total = 0
    overall_unique = 0
    for namespace in MODEL_NAMESPACES:
        total = 0
        unique = 0
        samples = 0
        for manifest_path in sorted(
            (RESULTS_ROOT / namespace).glob("*/manifest.json")
        ):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("evaluation_protocol") != "poc_trace_per_submission_v2":
                continue
            stats, representatives = deduplicate_submission_attempts(
                manifest.get("submission_attempts") or []
            )
            manifest["poc_deduplication"] = stats
            manifest["deduplicated_pocs"] = representatives
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False, default=str)
                + "\n",
                encoding="utf-8",
            )
            samples += 1
            total += stats["total_poc_submissions"]
            unique += stats["deduplicated_poc_count"]

        duplicates = total - unique
        aggregate["models"][namespace] = {
            "samples": samples,
            "total_poc_submissions": total,
            "deduplicated_poc_count": unique,
            "duplicate_poc_submissions": duplicates,
            "deduplicated_ratio": round(unique / total, 6) if total else None,
            "duplicate_ratio": round(duplicates / total, 6) if total else None,
        }
        overall_total += total
        overall_unique += unique

    overall_duplicates = overall_total - overall_unique
    aggregate["overall"] = {
        "samples": sum(
            item["samples"] for item in aggregate["models"].values()
        ),
        "total_poc_submissions": overall_total,
        "deduplicated_poc_count": overall_unique,
        "duplicate_poc_submissions": overall_duplicates,
        "deduplicated_ratio": (
            round(overall_unique / overall_total, 6)
            if overall_total
            else None
        ),
        "duplicate_ratio": (
            round(overall_duplicates / overall_total, 6)
            if overall_total
            else None
        ),
    }
    output = RESULTS_ROOT / "poc_deduplication_report.json"
    output.write_text(
        json.dumps(aggregate, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(aggregate, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
