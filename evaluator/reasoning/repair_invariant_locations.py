#!/usr/bin/env python3
"""Repair missing invariant locations from already-frozen GT evidence.

This is a metadata-only normalization pass.  It never executes a testcase and
never guesses a source location:

* missing root fields come from ``ground_truth.root_cause``;
* missing edge endpoints come from the exact ``from_step``/``to_step`` entries
  in ``ground_truth.fine_trace``.

Existing non-empty fields are treated as immutable consistency checks.  The
default mode is a dry run; pass ``--apply`` to write validated repairs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"
LOCATION_FIELDS = ("file", "function", "line")


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _copy_missing(
    destination: dict[str, Any],
    source: dict[str, Any],
    fields: tuple[str, ...],
    *,
    sample_id: str,
    context: str,
) -> list[str]:
    changed: list[str] = []
    for field in fields:
        current = destination.get(field)
        evidence = source.get(field)
        if _present(current):
            if _present(evidence) and current != evidence:
                raise ValueError(
                    f"{sample_id} {context}: existing {field}={current!r} "
                    f"conflicts with frozen evidence {evidence!r}"
                )
            continue
        if not _present(evidence):
            raise ValueError(
                f"{sample_id} {context}: missing frozen evidence for {field}"
            )
        destination[field] = evidence
        changed.append(field)
    return changed


def repair_package(gt_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    gt = _load(gt_dir / "ground_truth.json")
    invariants = _load(gt_dir / "verified_invariants.json")
    sample_id = str(gt.get("sample_id") or gt_dir.name)
    repairs: list[dict[str, Any]] = []

    root = invariants.get("root_cause_criterion")
    if isinstance(root, dict) and any(
        not _present(root.get(field)) for field in LOCATION_FIELDS
    ):
        changed = _copy_missing(
            root,
            gt.get("root_cause") or {},
            LOCATION_FIELDS,
            sample_id=sample_id,
            context="root_cause_criterion",
        )
        if changed:
            repairs.append({
                "kind": "root",
                "invariant_id": root.get("invariant_id"),
                "fields": changed,
            })

    fine_steps: dict[int, dict[str, Any]] = {}
    for step in gt.get("fine_trace", []):
        if not isinstance(step, dict) or not isinstance(step.get("step"), int):
            continue
        number = int(step["step"])
        if number in fine_steps:
            raise ValueError(f"{sample_id}: duplicate fine-trace step {number}")
        fine_steps[number] = step

    for edge in invariants.get("edges", []):
        if not isinstance(edge, dict) or edge.get("verified") is False:
            continue
        endpoint_fields = tuple(
            f"{prefix}_{field}"
            for prefix in ("from", "to")
            for field in LOCATION_FIELDS
        )
        if all(_present(edge.get(field)) for field in endpoint_fields):
            continue
        edge_id = str(edge.get("invariant_id") or "<missing-id>")
        changed: list[str] = []
        for prefix in ("from", "to"):
            step_number = edge.get(f"{prefix}_step")
            if not isinstance(step_number, int) or step_number not in fine_steps:
                raise ValueError(
                    f"{sample_id} {edge_id}: cannot resolve {prefix} endpoint "
                    f"from fine-trace step {step_number!r}"
                )
            evidence = {
                f"{prefix}_{field}": fine_steps[step_number].get(field)
                for field in LOCATION_FIELDS
            }
            changed.extend(_copy_missing(
                edge,
                evidence,
                tuple(evidence),
                sample_id=sample_id,
                context=f"edge {edge_id} {prefix}",
            ))
        if changed:
            repairs.append({
                "kind": "edge",
                "invariant_id": edge_id,
                "fields": changed,
            })

    return invariants, repairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gt-results", type=Path, default=GT_RESULTS)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    packages = 0
    changed_packages = 0
    root_repairs = 0
    edge_repairs = 0
    repaired_fields = 0
    for gt_dir in sorted(path for path in args.gt_results.iterdir() if path.is_dir()):
        gt_path = gt_dir / "ground_truth.json"
        invariant_path = gt_dir / "verified_invariants.json"
        if not gt_path.is_file() or not invariant_path.is_file():
            continue
        packages += 1
        invariants, repairs = repair_package(gt_dir)
        if not repairs:
            continue
        changed_packages += 1
        root_repairs += sum(item["kind"] == "root" for item in repairs)
        edge_repairs += sum(item["kind"] == "edge" for item in repairs)
        repaired_fields += sum(len(item["fields"]) for item in repairs)
        print(json.dumps({"sample_id": gt_dir.name, "repairs": repairs}))
        if args.apply:
            invariant_path.write_text(
                json.dumps(invariants, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "packages_checked": packages,
        "changed_packages": changed_packages,
        "root_invariants_repaired": root_repairs,
        "edges_repaired": edge_repairs,
        "fields_repaired": repaired_fields,
    }, indent=2))


if __name__ == "__main__":
    main()
