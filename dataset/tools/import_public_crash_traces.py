#!/usr/bin/env python3
"""Import public crash context for non-ARVO samples.

Conservative policy:
- keep existing default_crash_trace_path values;
- prefer local public PoC/bug-report artifacts when they contain sanitizer text;
- for OSS-Fuzz OSV records, use the public OSV API `details` block only when it
  contains `Crash type:` and `Crash state:`;
- never synthesize a trace from summaries, CVE descriptions, or patch diffs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTION = REPO_ROOT / "dataset" / "selected_1000.json"
TRACE_ROOT = REPO_ROOT / "dataset" / "crash_traces"

import sys
sys.path.insert(0, str(REPO_ROOT / "gt_generation"))
from gt_toolkit.prepare import _extract_sanitizer_trace_text  # noqa: E402


def source_family(sample: dict) -> str:
    return str(sample.get("source_family") or "").lower()


def trace_candidates(sample: dict) -> list[Path]:
    sample_id = str(sample.get("sample_id") or "")
    candidates: list[Path] = []
    raw_paths = [sample.get("poc_path")]
    poc_dir = REPO_ROOT / "dataset" / "pocs" / sample_id
    if poc_dir.is_dir():
        raw_paths.extend(
            sorted(
                (path for path in poc_dir.iterdir() if path.is_file() and path.name != "patch.diff"),
                key=lambda path: (path.name != "poc", path.name),
            )
        )
    for raw in raw_paths:
        if not raw:
            continue
        path = raw if isinstance(raw, Path) else Path(str(raw)).expanduser()
        path_candidates = [path]
        if not path.is_absolute():
            path_candidates.extend((REPO_ROOT / path, REPO_ROOT / "dataset" / path))
        for resolved in path_candidates:
            if resolved not in candidates:
                candidates.append(resolved)
    return candidates


def local_sanitizer_trace(sample: dict) -> tuple[str, str]:
    for candidate in trace_candidates(sample):
        if not candidate.is_file():
            continue
        try:
            raw = candidate.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        trace = _extract_sanitizer_trace_text(raw.decode("utf-8", errors="replace"))
        if trace:
            return trace, str(candidate.relative_to(REPO_ROOT))
    return "", ""


def osv_details_trace(sample: dict) -> tuple[str, str]:
    vuln_id = str(sample.get("benchmark_id") or sample.get("public_id") or "")
    if not vuln_id.startswith("OSV-"):
        return "", ""
    url = f"https://api.osv.dev/v1/vulns/{vuln_id}"
    try:
        request = Request(url, headers={"User-Agent": "gt-generation-public-trace-import"})
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return "", ""
    details = str(data.get("details") or "").strip()
    if "Crash type:" not in details or "Crash state:" not in details:
        return "", ""
    return details + "\n", url


def write_trace(sample: dict, trace: str, trace_dir: Path, source: str, source_kind: str) -> None:
    sample_id = str(sample["sample_id"])
    path = trace_dir / f"{sample_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trace.rstrip() + "\n", encoding="utf-8")
    sample["default_crash_trace_path"] = str(path.relative_to(REPO_ROOT))
    sample["trace_source"] = source
    sample["trace_source_kind"] = source_kind


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, default=SELECTION)
    parser.add_argument("--trace-root", type=Path, default=TRACE_ROOT)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    selection_path = args.selection.resolve()
    trace_root = args.trace_root.resolve()
    samples = json.loads(selection_path.read_text(encoding="utf-8"))
    if not isinstance(samples, list):
        raise SystemExit(f"selection must be a JSON list: {selection_path}")

    imported: dict[str, int] = {}
    remaining: dict[str, int] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        family = source_family(sample)
        sample_id = str(sample.get("sample_id") or "")
        if family == "arvo" or sample_id.startswith("arvo_"):
            continue
        if sample.get("default_crash_trace_path") and not args.overwrite:
            continue

        trace, source = local_sanitizer_trace(sample)
        source_kind = "local_public_poc_sanitizer_report"
        if not trace and family == "osv":
            trace, source = osv_details_trace(sample)
            source_kind = "osv_public_crash_state"
        if trace:
            imported[family] = imported.get(family, 0) + 1
            if not args.dry_run:
                write_trace(sample, trace, trace_root / family, source, source_kind)
        else:
            remaining[family] = remaining.get(family, 0) + 1

    if not args.dry_run:
        selection_path.write_text(
            json.dumps(samples, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "imported": imported,
                "remaining_without_trace": remaining,
                "trace_root": str(trace_root),
                "dry_run": args.dry_run,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
