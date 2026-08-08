"""Reject invalid Stage-04 assertion plans before instrumentation starts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .assertions import (
    validate_binding_coverage,
    validate_frozen_spec,
    validate_invariant_bindings,
)
from .evidence import file_sha256
from .package_audit import _verified_invariant_harness_errors


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return value


def run_preflight(
    spec_path: Path,
    invariants_path: Path,
    field_bindings_path: Path,
    event_locations_path: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        spec = _load(spec_path)
        validate_frozen_spec(spec)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"invalid assertion spec: {exc}"]}

    try:
        invariants = _load(invariants_path)
        field_bindings = _load(field_bindings_path).get("bindings", {})
        event_locations = _load(event_locations_path).get("locations", {})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"invalid assertion side map: {exc}"]}

    binding = validate_invariant_bindings(invariants, spec)
    errors.extend(binding["errors"])
    errors.extend(_verified_invariant_harness_errors(invariants))
    coverage = validate_binding_coverage(spec, field_bindings, event_locations)
    errors.extend(coverage["errors"])
    return {
        "schema_version": "assertion-preflight-v1",
        "sample_id": spec.get("sample_id"),
        "assertion_content_hash": spec.get("content_hash"),
        "input_hashes": {
            spec_path.name: file_sha256(spec_path),
            invariants_path.name: file_sha256(invariants_path),
            field_bindings_path.name: file_sha256(field_bindings_path),
            event_locations_path.name: file_sha256(event_locations_path),
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ok": not errors,
        "errors": errors,
        "invariant_binding": binding,
        "binding_coverage": coverage,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--candidate-invariants", type=Path, required=True)
    parser.add_argument("--field-bindings", type=Path, required=True)
    parser.add_argument("--event-locations", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_preflight(
        args.spec,
        args.candidate_invariants,
        args.field_bindings,
        args.event_locations,
    )
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
