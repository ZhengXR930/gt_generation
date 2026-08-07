"""Audit a completed GT result directory as a self-contained data package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .assertions import (
    assertion_content_hash,
    validate_frozen_spec,
    validate_invariant_bindings,
)
from .validate import harness_location_reason, validate_data


REQUIRED_FILES = (
    "sample_info.json",
    "build.sh",
    "poc",
    "ground_truth.json",
    "verified_invariants.json",
    "verified_assertions.json",
    "assertion_results.json",
    "perturbation_results.json",
    "reachability_report.json",
)

REACHABILITY_FIELDS = (
    "reachability_checked",
    "R1_parser_admitted",
    "R2_source_reached",
    "R3_root_cause_function_reached",
    "R4_root_cause_line_reached",
    "R3_sink_function_reached",
    "R4_sink_line_reached",
    "R5_sanitizer_triggered",
)


def _load_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"unreadable JSON {path.name}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path.name} must contain one JSON object")
        return {}
    return value


def _public_issue(sample_info: dict[str, Any]) -> str:
    direct = sample_info.get("original_bug_description")
    if direct:
        return str(direct).strip()
    issue = sample_info.get("issue_description")
    if isinstance(issue, str):
        return issue.strip()
    if isinstance(issue, dict):
        return str(issue.get("original") or "").strip()
    return ""


def _has_default_crash_trace(sample_info: dict[str, Any], result_dir: Path) -> bool:
    if (result_dir / "default_crash_trace.txt").is_file():
        return bool(
            (result_dir / "default_crash_trace.txt")
            .read_text(encoding="utf-8", errors="replace")
            .strip()
        )
    if str(sample_info.get("default_crash_trace") or "").strip():
        return True
    trace_path = str(sample_info.get("default_crash_trace_path") or "").strip()
    return bool(trace_path and (result_dir / trace_path).is_file())


def _artifact_reference_errors(
    report: dict[str, Any], result_dir: Path
) -> list[str]:
    errors: list[str] = []
    root = result_dir.resolve()
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, dict):
        return errors
    for name, raw_path in artifacts.items():
        if not isinstance(raw_path, str) or not raw_path.strip():
            errors.append(f"reachability artifact {name} has an invalid path")
            continue
        candidate = (result_dir / raw_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            errors.append(f"reachability artifact {name} escapes result directory: {raw_path}")
            continue
        if not candidate.is_file():
            errors.append(f"reachability artifact {name} is missing: {raw_path}")
    return errors


def _verified_invariant_harness_errors(verified_invariants: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def check(where: str, loc: dict[str, Any]) -> None:
        reason = harness_location_reason(loc)
        if reason:
            errors.append(
                f"verified_invariants.json {where} is anchored in unscored "
                f"fuzzing harness code: {reason}"
            )

    criterion = verified_invariants.get("root_cause_criterion")
    if isinstance(criterion, dict):
        check("root_cause_criterion", criterion)

    for index, node in enumerate(verified_invariants.get("nodes", [])):
        if isinstance(node, dict):
            check(f"nodes[{index}]", node)

    for index, edge in enumerate(verified_invariants.get("edges", [])):
        if not isinstance(edge, dict):
            continue
        check(
            f"edges[{index}].from",
            {
                "file": edge.get("from_file"),
                "function": edge.get("from_function"),
                "line": edge.get("from_line"),
            },
        )
        check(
            f"edges[{index}].to",
            {
                "file": edge.get("to_file"),
                "function": edge.get("to_function"),
                "line": edge.get("to_line"),
            },
        )

    return errors


def audit_package(result_dir: Path) -> dict[str, Any]:
    result_dir = result_dir.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    for name in REQUIRED_FILES:
        path = result_dir / name
        if not path.is_file():
            errors.append(f"missing required file: {name}")
        elif path.stat().st_size == 0 and name != "poc":
            # A zero-byte poc is a legitimate empty-input crash (some fuzzers,
            # e.g. lz4 round_trip_fuzzer, trigger on empty input; ARVO ships the
            # canonical poc as 0 bytes). Stage 01 independently verifies the crash
            # reproduces, so poc content is proven by reproduction, not file size.
            errors.append(f"empty required file: {name}")
    if errors:
        return {
            "result_dir": str(result_dir),
            "ok": False,
            "errors": errors,
            "warnings": warnings,
        }

    json_names = [name for name in REQUIRED_FILES if name.endswith(".json")]
    documents = {
        name: _load_json(result_dir / name, errors) for name in json_names
    }
    sample_ids = {
        name: str(document.get("sample_id") or "")
        for name, document in documents.items()
    }
    expected_sample_id = sample_ids.get("ground_truth.json", "")
    if not expected_sample_id:
        errors.append("ground_truth.json has no sample_id")
    for name, sample_id in sample_ids.items():
        if sample_id != expected_sample_id:
            errors.append(
                f"sample_id mismatch in {name}: {sample_id!r} != {expected_sample_id!r}"
            )

    gt = documents["ground_truth.json"]
    gt_report = validate_data(gt, ground_truth=str(result_dir / "ground_truth.json"))
    errors.extend(f"ground_truth: {message}" for message in gt_report.errors)
    warnings.extend(f"ground_truth: {message}" for message in gt_report.warnings)

    sample_info = documents["sample_info.json"]
    if not _public_issue(sample_info):
        errors.append("sample_info.json has no exact public issue description")
    if not _has_default_crash_trace(sample_info, result_dir):
        errors.append(
            "no exact default crash trace in default_crash_trace.txt or sample_info.json"
        )

    verified = documents["verified_assertions.json"]
    assertion_results = documents["assertion_results.json"]
    verified_schema = str(verified.get("schema_version") or "")
    if verified_schema != "verified-assertions-v3":
        errors.append(
            "verified_assertions.json must use verified-assertions-v3; "
            "legacy assertions do not prove invariant edges"
        )
    suffix = "v3" if verified_schema == "verified-assertions-v3" else "v2"
    reconstructed_spec = {
        "schema_version": f"assertion-spec-{suffix}",
        "sample_id": verified.get("sample_id"),
        "original_case": assertion_results.get("original_case", "original"),
        "assertions": verified.get("assertions", []),
    }
    reconstructed_spec["content_hash"] = verified.get("content_hash")
    try:
        validate_frozen_spec(reconstructed_spec)
    except ValueError as exc:
        errors.append(f"verified assertion spec cannot be reconstructed: {exc}")
    if assertion_results.get("candidate_content_hash") != assertion_content_hash(
        reconstructed_spec
    ):
        errors.append("assertion_results candidate_content_hash does not match verified spec")
    if assertion_results.get("all_verified") is not True:
        errors.append("assertion_results.json all_verified is not true")
    if not any(
        isinstance(assertion, dict) and assertion.get("kind") == "required"
        for assertion in verified.get("assertions", [])
    ):
        errors.append(
            "verified_assertions.json has no required root-obligation assertion"
        )

    binding = validate_invariant_bindings(
        documents["verified_invariants.json"], reconstructed_spec
    )
    errors.extend(f"invariant binding: {message}" for message in binding["errors"])
    errors.extend(_verified_invariant_harness_errors(documents["verified_invariants.json"]))

    perturbations = documents["perturbation_results.json"]
    if perturbations.get("all_needed_witnessed") is not True:
        errors.append("perturbation_results.json has an unwitnessed required perturbation")

    reachability = documents["reachability_report.json"]
    for field in REACHABILITY_FIELDS:
        if reachability.get(field) is not True:
            errors.append(f"reachability_report.json {field} is not true")
    errors.extend(_artifact_reference_errors(reachability, result_dir))

    return {
        "result_dir": str(result_dir),
        "sample_id": expected_sample_id,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit_package(args.result_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
