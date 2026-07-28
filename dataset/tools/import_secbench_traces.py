#!/usr/bin/env python3
"""Import SEC-bench sanitizer reports into the local sample metadata.

The public Hugging Face dataset `SEC-bench/SEC-bench` exposes a
`sanitizer_report` field for each instance. This script writes those reports as
local default crash traces and points `dataset/selected_1000.json` at them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTION = REPO_ROOT / "dataset" / "selected_1000.json"
TRACE_DIR = REPO_ROOT / "dataset" / "crash_traces" / "secbench"
HF_BASE = "https://huggingface.co/datasets/SEC-bench/SEC-bench/resolve/main/"
HF_FILES = ("data/eval-cve.jsonl", "data/eval-oss.jsonl")


def fetch_jsonl(name: str) -> list[dict]:
    request = Request(HF_BASE + name, headers={"User-Agent": "gt-generation-secbench-import"})
    with urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_hf_rows() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for name in HF_FILES:
        for row in fetch_jsonl(name):
            instance_id = str(row.get("instance_id") or "")
            if instance_id:
                rows[instance_id] = row
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, default=SELECTION)
    parser.add_argument("--trace-dir", type=Path, default=TRACE_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    selection_path = args.selection.resolve()
    trace_dir = args.trace_dir.resolve()
    samples = json.loads(selection_path.read_text(encoding="utf-8"))
    if not isinstance(samples, list):
        raise SystemExit(f"selection must be a JSON list: {selection_path}")

    hf_rows = load_hf_rows()
    matched = 0
    updated = 0
    missing: list[str] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        if str(sample.get("source_family") or "").lower() != "secbench":
            continue
        sample_id = str(sample.get("sample_id") or "")
        benchmark_id = str(sample.get("benchmark_id") or "")
        report = str((hf_rows.get(benchmark_id) or {}).get("sanitizer_report") or "").strip()
        if not sample_id or not report:
            missing.append(sample_id or benchmark_id)
            continue
        matched += 1
        trace_path = trace_dir / f"{sample_id}.txt"
        relative_trace_path = trace_path.relative_to(REPO_ROOT)
        if not args.dry_run:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text(report + "\n", encoding="utf-8")
        before = (
            sample.get("default_crash_trace_path"),
            sample.get("trace_source"),
        )
        sample["default_crash_trace_path"] = str(relative_trace_path)
        sample["trace_source"] = "huggingface:SEC-bench/SEC-bench.sanitizer_report"
        if before != (sample["default_crash_trace_path"], sample["trace_source"]):
            updated += 1

    if not args.dry_run:
        selection_path.write_text(
            json.dumps(samples, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "matched_secbench": matched,
                "updated_metadata": updated,
                "missing_reports": missing,
                "trace_dir": str(trace_dir),
                "dry_run": args.dry_run,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
