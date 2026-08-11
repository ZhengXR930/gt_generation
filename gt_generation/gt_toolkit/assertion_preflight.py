"""Reject invalid Stage-04 assertion plans before instrumentation starts."""

from __future__ import annotations

import argparse
import json
import subprocess
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


def _validate_patch_syntax(path: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "apply", "--numstat", "--", str(path.resolve())],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError as exc:
        return [f"cannot validate instrumentation patch {path.name}: {exc}"]
    if completed.returncode == 0 and completed.stdout.strip():
        return []
    detail = (completed.stderr or completed.stdout).strip()
    if not detail:
        detail = "patch contains no file changes"
    return [f"invalid instrumentation patch {path.name}: {detail}"]


def run_preflight(
    spec_path: Path,
    invariants_path: Path,
    field_bindings_path: Path,
    event_locations_path: Path,
    vulnerable_instrumentation_path: Path | None = None,
    fixed_instrumentation_path: Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        spec = _load(spec_path)
        validate_frozen_spec(spec)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"invalid assertion spec: {exc}"]}

    try:
        invariants = _load(invariants_path)
        field_bindings_doc = _load(field_bindings_path)
        event_locations_doc = _load(event_locations_path)
        field_bindings = field_bindings_doc.get("bindings", {})
        event_locations = event_locations_doc.get("locations", {})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"invalid assertion side map: {exc}"]}
    for name, document in (
        ("candidate_invariants.json", invariants),
        ("field_bindings.json", field_bindings_doc),
        ("event_locations.json", event_locations_doc),
    ):
        if "schema_version" in document:
            errors.append(f"{name} must not contain artifact-level schema_version")

    binding = validate_invariant_bindings(invariants, spec)
    errors.extend(binding["errors"])
    errors.extend(_verified_invariant_harness_errors(invariants))
    coverage = validate_binding_coverage(spec, field_bindings, event_locations)
    errors.extend(coverage["errors"])
    input_paths = (
        spec_path,
        invariants_path,
        field_bindings_path,
        event_locations_path,
    )
    optional_paths = (
        vulnerable_instrumentation_path,
        fixed_instrumentation_path,
    )
    for path in optional_paths:
        if path is not None and not path.is_file():
            errors.append(f"missing instrumentation plan: {path.name}")
        elif path is not None:
            errors.extend(_validate_patch_syntax(path))
    committed_paths = input_paths + tuple(
        path for path in optional_paths if path is not None and path.is_file()
    )
    return {
        "schema_version": "assertion-preflight-v1",
        "sample_id": spec.get("sample_id"),
        "assertion_content_hash": spec.get("content_hash"),
        "input_hashes": {
            path.name: file_sha256(path)
            for path in committed_paths
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
    parser.add_argument("--vulnerable-instrumentation", type=Path)
    parser.add_argument("--fixed-instrumentation", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_preflight(
        args.spec,
        args.candidate_invariants,
        args.field_bindings,
        args.event_locations,
        args.vulnerable_instrumentation,
        args.fixed_instrumentation,
    )
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
