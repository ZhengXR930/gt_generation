#!/usr/bin/env python3
"""Remove obsolete ``depends_on`` fields from parsed coding-agent traces.

Raw model responses are deliberately left untouched as provenance. The migration
only rewrites validated JSON artifacts used by evaluation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOTS = (
    REPO_ROOT / "poc_generation" / "poc_results",
    REPO_ROOT
    / "poc_generation"
    / "poc_generator"
    / "server"
    / "logs"
    / "submissions",
)
TRACE_FILENAMES = {"fine_trace.json", "candidate_trace.json"}


def trace_paths(roots: list[Path]) -> list[Path]:
    return sorted(
        path
        for root in roots
        if root.is_dir()
        for path in root.rglob("*.json")
        if path.name in TRACE_FILENAMES
    )


def strip_dependencies(path: Path, *, check: bool) -> tuple[int, bool]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected a JSON array")

    removed = 0
    for index, step in enumerate(value, 1):
        if not isinstance(step, dict):
            raise ValueError(f"{path}: step {index} is not an object")
        if "depends_on" in step:
            removed += 1
            if not check:
                del step["depends_on"]

    if removed and not check:
        path.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return removed, bool(removed and not check)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        help="Roots to scan (defaults to model results and parsed server submissions)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report obsolete fields without modifying files",
    )
    args = parser.parse_args()

    paths = trace_paths(args.roots or list(DEFAULT_ROOTS))
    files_with_dependencies = 0
    fields = 0
    changed = 0
    for path in paths:
        removed, rewritten = strip_dependencies(path, check=args.check)
        fields += removed
        files_with_dependencies += int(removed > 0)
        changed += int(rewritten)

    print(
        json.dumps(
            {
                "files_scanned": len(paths),
                "files_with_depends_on": files_with_dependencies,
                "depends_on_fields": fields,
                "files_changed": changed,
                "check_only": args.check,
            },
            sort_keys=True,
        )
    )
    return 1 if args.check and fields else 0


if __name__ == "__main__":
    raise SystemExit(main())
