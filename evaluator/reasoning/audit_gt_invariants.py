#!/usr/bin/env python3
"""Audit every frozen GT invariant package and its evaluator compatibility."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

from gt_generation.gt_toolkit.assertions import (
    annotate_scored_invariants,
    validate_binding_coverage,
)
from gt_generation.gt_toolkit.package_audit import audit_package

from .scoring import _distinctive, build_invariant_checklist


REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"
OUTPUT = Path(__file__).resolve().parent / "gt_invariant_audit_report.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    candidate_dirs = sorted(
        path
        for path in GT_RESULTS.iterdir()
        if path.is_dir() and (path / "ground_truth.json").is_file()
    )
    invariant_package_files = (
        "verified_invariants.json",
        "verified_assertions.json",
        "field_bindings.json",
        "event_locations.json",
    )
    incomplete_packages = [
        {
            "sample_id": path.name,
            "missing": [
                name for name in invariant_package_files if not (path / name).is_file()
            ],
        }
        for path in candidate_dirs
        if not all((path / name).is_file() for name in invariant_package_files)
    ]
    sample_dirs = [
        path
        for path in candidate_dirs
        if all((path / name).is_file() for name in invariant_package_files)
    ]
    totals = Counter()
    invariant_schemas = Counter()
    edge_roles = Counter()
    package_failures = []
    package_warnings = []
    binding_errors = []
    binding_warnings = []
    semantic_gaps = []
    scorer_gaps = []

    for result_dir in sample_dirs:
        sample_id = result_dir.name
        package = audit_package(result_dir)
        if not package["ok"]:
            package_failures.append(
                {"sample_id": sample_id, "errors": package["errors"]}
            )
        if package["warnings"]:
            package_warnings.append(
                {"sample_id": sample_id, "warnings": package["warnings"]}
            )

        gt = _load(result_dir / "ground_truth.json")
        invariants = _load(result_dir / "verified_invariants.json")
        assertions = _load(result_dir / "verified_assertions.json")
        field_bindings = (
            _load(result_dir / "field_bindings.json").get("bindings") or {}
        )
        event_locations = (
            _load(result_dir / "event_locations.json").get("locations") or {}
        )
        invariant_schemas[str(invariants.get("schema_version"))] += 1

        nodes = invariants.get("nodes") or []
        edges = invariants.get("edges") or []
        root = invariants.get("root_cause_criterion")
        assertion_items = assertions.get("assertions") or []
        totals["samples"] += 1
        totals["nodes"] += len(nodes)
        totals["edges"] += len(edges)
        totals["root_cause_criteria"] += int(isinstance(root, dict))
        totals["assertions"] += len(assertion_items)
        for assertion in assertion_items:
            totals[f"assertion_{assertion.get('kind')}"] += 1

        coverage = validate_binding_coverage(
            {"schema_version": "assertion-spec-v3", "assertions": assertion_items},
            field_bindings,
            event_locations,
        )
        if coverage["errors"]:
            binding_errors.append(
                {"sample_id": sample_id, "errors": coverage["errors"]}
            )
        if coverage["warnings"]:
            binding_warnings.append(
                {"sample_id": sample_id, "warnings": coverage["warnings"]}
            )

        annotated = annotate_scored_invariants(
            copy.deepcopy(invariants), assertions, field_bindings
        )
        for edge in annotated.get("edges", []):
            edge_roles[str(edge.get("scored_role") or "unclassified")] += 1

        if not isinstance(root, dict) or not any(
            str(root.get(key) or "").strip()
            for key in (
                "description",
                "criterion",
                "obligation",
                "missing_conjunct",
                "variable",
            )
        ):
            semantic_gaps.append(
                {
                    "sample_id": sample_id,
                    "invariant_id": (
                        root.get("invariant_id")
                        if isinstance(root, dict)
                        else None
                    ),
                    "section": "root_cause_criterion",
                    "issue": "no semantic statement or operand",
                }
            )
        for edge in edges:
            if not any(
                str(edge.get(key) or "").strip()
                for key in ("relation", "description", "condition", "note")
            ):
                semantic_gaps.append(
                    {
                        "sample_id": sample_id,
                        "invariant_id": edge.get("invariant_id"),
                        "section": "edge",
                        "issue": (
                            "no narrative relation; executable assertion and "
                            "field bindings remain available"
                        ),
                    }
                )

        for item in build_invariant_checklist(sample_id):
            if not item["functions"]:
                scorer_gaps.append(
                    {
                        "sample_id": sample_id,
                        "invariant_id": item["id"],
                        "issue": "current Tier-1 checklist has no function",
                    }
                )
            tokens = _distinctive(item["id_tokens"]) or item["id_tokens"]
            if not tokens:
                scorer_gaps.append(
                    {
                        "sample_id": sample_id,
                        "invariant_id": item["id"],
                        "issue": "current Tier-1 checklist has no identifier token",
                    }
                )

    selected_invariants = (
        totals["nodes"] + totals["edges"] + totals["root_cause_criteria"]
    )
    report = {
        "summary": {
            **dict(totals),
            "ground_truth_directories": len(candidate_dirs),
            "incomplete_invariant_packages": len(incomplete_packages),
            "selected_invariants": selected_invariants,
            "package_audit_passed": totals["samples"] - len(package_failures),
            "package_audit_failed": len(package_failures),
            "package_warning_samples": len(package_warnings),
            "binding_error_samples": len(binding_errors),
            "binding_warning_samples": len(binding_warnings),
            "semantic_gap_count": len(semantic_gaps),
            "current_scorer_gap_count": len(scorer_gaps),
        },
        "invariant_schema_versions": dict(invariant_schemas),
        "evaluation_selection": {
            "reasoning_edges": edge_roles["reasoning"],
            "connectivity_edges_not_for_understanding_score": edge_roles[
                "connectivity"
            ],
            "mechanism_nodes": sum(
                1
                for result_dir in sample_dirs
                for node in annotate_scored_invariants(
                    copy.deepcopy(
                        _load(result_dir / "verified_invariants.json")
                    ),
                    _load(result_dir / "verified_assertions.json"),
                    (
                        _load(result_dir / "field_bindings.json").get(
                            "bindings"
                        )
                        or {}
                    ),
                ).get("nodes", [])
                if node.get("scored") is True
            ),
        },
        "package_failures": package_failures,
        "incomplete_packages": incomplete_packages,
        "package_warnings": package_warnings,
        "binding_errors": binding_errors,
        "binding_warnings": binding_warnings,
        "semantic_gaps": semantic_gaps,
        "current_scorer_gaps": scorer_gaps,
        "conclusion": (
            "A package-audit pass establishes schema consistency, unique and "
            "complete invariant/assertion bindings, verified runtime predicates, "
            "required-root differential evidence, perturbation witnesses, and "
            "R1-R5 GT reachability. Semantic gaps and current-scorer gaps are "
            "reported separately and do not silently invalidate the GT."
        ),
    }
    OUTPUT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print(json.dumps(report["evaluation_selection"], indent=2))
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
