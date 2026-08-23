"""Audit a completed GT result directory as a self-contained data package."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .assertions import (
    build_assertion_reward_spec,
    assertion_content_hash,
    _load_assertion_reward_module,
    validate_frozen_spec,
    validate_invariant_bindings,
)
from .context_trace import context_trace_errors
from .evidence import commitment_errors
from .prepare import RUNTIME_ARCHIVE_NAMES, RUNTIME_BUILD_RECIPE_NAME
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
    "field_bindings.json",
    "event_locations.json",
    "reachability_report.json",
)

OPTIONAL_PROJECTION_FILES = (
    "assertion_reward_spec.json",
    "context_trace.json",
    "runtime_build.json",
    "runtime_materials.json",
    "portability_report.json",
    "runtime_work_manifest.json",
)

REACHABILITY_FIELDS = (
    "reachability_checked",
    "R1_parser_admitted",
    "R2_source_reached",
    "R3_root_cause_function_reached",
    "R5_sanitizer_triggered",
)


def _is_arvo_sample_id(sample_id: str) -> bool:
    return sample_id.startswith("arvo_")


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


def _gt_generation_reachability_gate(report: dict[str, Any]) -> bool:
    """Accept GT packages whose sink is proven by the frozen assertion trace.

    Candidate evaluation remains strict location-reachability-v3. Package audit
    has Stage 04 runtime evidence too, so a verified sink assertion event may
    stand in for an exact gdb sink-line hit when the debugger lands only on the
    sink function.  The GT generator also has stronger evidence than the public
    evaluator: the root required assertion is already proven differentially and
    the sanitizer trace proves the behavior.  Do not reject a package only
    because an auxiliary parser/source breakpoint did not bind cleanly when the
    root/sink path and sanitizer are established.
    """
    if report.get("reachability_checked") is not True:
        return False
    if not (
        report.get("target_vulnerability_triggered") is True
        or report.get("raw_target_vulnerability_triggered") is True
        or report.get("R5_sanitizer_triggered") is True
    ):
        return False
    raw_hits = report.get("raw_location_hits")
    if not isinstance(raw_hits, dict):
        raw_hits = {}
    hit_locations = [
        hit for hit in report.get("hit_locations", []) if isinstance(hit, dict)
    ]
    event_reachability = report.get("assertion_event_reachability")
    if not isinstance(event_reachability, dict):
        event_reachability = {}

    def hit_location_reached(*kinds: str) -> bool:
        expected = set(kinds)
        for hit in hit_locations:
            if hit.get("kind") not in expected:
                continue
            if hit.get("breakpoint_error"):
                continue
            if hit.get("hit_count") == 0:
                continue
            return True
        return False

    def assertion_role_reached(role: str) -> bool:
        for hit in hit_locations:
            if hit.get("kind") != "assertion_event":
                continue
            roles = set(hit.get("assertion_role") or [])
            if role not in roles:
                continue
            event_point = str(hit.get("event_point") or "")
            if event_reachability.get(event_point) is True:
                return True
        return False

    source_or_later_reached = any(
        item is True
        for item in (
            report.get("R2_source_reached"),
            raw_hits.get("source"),
            report.get("R3_root_cause_reached"),
            report.get("R3_root_cause_function_reached"),
            raw_hits.get("root_cause"),
        )
    ) or hit_location_reached(
        "source",
        "root_cause",
        "root_cause_function",
        "sink_function",
    ) or assertion_role_reached("root")
    if not source_or_later_reached:
        return False
    if not (
        report.get("R3_root_cause_reached") is True
        or report.get("R3_root_cause_function_reached") is True
        or raw_hits.get("root_cause") is True
        or hit_location_reached("root_cause", "root_cause_function")
        or assertion_role_reached("root")
    ):
        return False
    if report.get("R4_sink_reached") is True or report.get("R4_sink_line_reached") is True:
        return True
    if raw_hits.get("sink") is True:
        return True
    if hit_location_reached("sink", "sink_line", "sink_function"):
        return True
    return assertion_role_reached("sink")


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


def _contract_errors(documents: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    gt = documents["ground_truth.json"]
    vi = documents["verified_invariants.json"]
    va = documents["verified_assertions.json"]
    fb = documents["field_bindings.json"]
    el = documents["event_locations.json"]
    for name, document in (
        ("ground_truth.json", gt),
        ("verified_invariants.json", vi),
        ("verified_assertions.json", va),
        ("field_bindings.json", fb),
        ("event_locations.json", el),
    ):
        if "schema_version" in document:
            errors.append(f"{name} must not contain artifact-level schema_version")
    if "coarse_trace" in gt:
        errors.append("ground_truth.json must not contain coarse_trace")
    for anchor_name in ("source", "root_cause", "sink"):
        anchor = gt.get(anchor_name)
        if not isinstance(anchor, dict):
            errors.append(f"ground_truth.json {anchor_name} must be an object")
            continue
        if not anchor.get("operands"):
            errors.append(f"ground_truth.json {anchor_name} missing operands")
    for anchor_name in ("root_cause", "sink"):
        relation = (gt.get(anchor_name) or {}).get("relation")
        if not isinstance(relation, dict) or not all(
            key in relation for key in ("op", "left", "right")
        ):
            errors.append(f"ground_truth.json {anchor_name} missing relation")

    nodes = [item for item in vi.get("nodes", []) if isinstance(item, dict)]
    edges = [item for item in vi.get("edges", []) if isinstance(item, dict)]
    node_ids = {str(node.get("invariant_id")) for node in nodes if node.get("invariant_id")}
    roles = {node.get("role") for node in nodes}
    if not {"source", "root_cause", "sink"} <= roles:
        errors.append("verified_invariants.json missing source/root_cause/sink nodes")
    criterion = vi.get("root_cause_criterion")
    criterion_id = (
        str(criterion.get("invariant_id") or "")
        if isinstance(criterion, dict)
        else ""
    )
    if not criterion_id or criterion_id not in node_ids:
        errors.append("verified_invariants.json root_cause_criterion does not point to a node")
    elif not any(
        node.get("role") == "root_cause"
        and str(node.get("invariant_id") or "") == criterion_id
        for node in nodes
    ):
        errors.append("verified_invariants.json root_cause_criterion target is not role=root_cause")
    for node in nodes:
        invariant_id = str(node.get("invariant_id") or "<missing>")
        if not node.get("operands"):
            errors.append(f"verified_invariants.json node {invariant_id} missing operands")
        if not isinstance(node.get("relation"), dict):
            errors.append(f"verified_invariants.json node {invariant_id} missing relation")
    for edge in edges:
        invariant_id = str(edge.get("invariant_id") or "<missing>")
        if edge.get("from_node") not in node_ids or edge.get("to_node") not in node_ids:
            errors.append(f"verified_invariants.json edge {invariant_id} has unresolved endpoints")
        if not edge.get("operands"):
            errors.append(f"verified_invariants.json edge {invariant_id} missing operands")
        if not isinstance(edge.get("relation"), dict):
            errors.append(f"verified_invariants.json edge {invariant_id} missing relation")
    selected = node_ids | {
        str(edge.get("invariant_id"))
        for edge in edges
        if edge.get("invariant_id")
    }
    for assertion in va.get("assertions", []):
        if not isinstance(assertion, dict):
            continue
        for invariant_id in assertion.get("invariants", []):
            if str(invariant_id) not in selected:
                errors.append(
                    "verified_assertions.json assertion "
                    f"{assertion.get('id')} references unselected invariant {invariant_id}"
                )
    return errors


def _runtime_spec_errors(
    spec: dict[str, Any], sample_id: str
) -> list[str]:
    errors: list[str] = []
    if spec.get("sample_id") != sample_id:
        errors.append(
            f"runtime_spec.json sample_id mismatch: {spec.get('sample_id')!r} != {sample_id!r}"
        )
    if spec.get("backend") != "local_workspace":
        errors.append("runtime_spec.json backend must be local_workspace for non-ARVO")
    if not str(spec.get("image") or "").strip():
        errors.append("runtime_spec.json missing image")
    if not str(spec.get("workdir") or "").startswith("/gt/"):
        errors.append("runtime_spec.json workdir must be under /gt")
    if not str(spec.get("executable") or "").strip():
        errors.append("runtime_spec.json missing executable")
    arguments = spec.get("arguments")
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        errors.append("runtime_spec.json arguments must be a string array")
    elif not any("{poc}" in arg for arg in arguments):
        errors.append("runtime_spec.json arguments must contain {poc}")
    environment = spec.get("environment")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        errors.append("runtime_spec.json environment must be an object of strings")
    if spec.get("input_placeholder") != "{poc}":
        errors.append("runtime_spec.json input_placeholder must be {poc}")
    if not str(spec.get("source") or "").strip():
        errors.append("runtime_spec.json missing source")
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
    json_names = [name for name in REQUIRED_FILES if name.endswith(".json")]
    json_names.extend(
        name for name in OPTIONAL_PROJECTION_FILES if (result_dir / name).is_file()
    )
    documents = {
        name: (
            _load_json(result_dir / name, errors)
            if (result_dir / name).is_file()
            else {}
        )
        for name in json_names
    }
    sample_ids = {
        name: str(document.get("sample_id") or "")
        for name, document in documents.items()
        if name != "assertion_reward_spec.json"
        and (result_dir / name).is_file()
    }
    expected_sample_id = sample_ids.get("ground_truth.json", "")
    if not expected_sample_id:
        errors.append("ground_truth.json has no sample_id")
    for name, sample_id in sample_ids.items():
        if sample_id != expected_sample_id:
            errors.append(
                f"sample_id mismatch in {name}: {sample_id!r} != {expected_sample_id!r}"
            )
    if expected_sample_id and not _is_arvo_sample_id(expected_sample_id):
        runtime_spec_path = result_dir / "runtime_spec.json"
        if not runtime_spec_path.is_file():
            errors.append("missing required file for non-ARVO sample: runtime_spec.json")
        else:
            runtime_spec = _load_json(runtime_spec_path, errors)
            errors.extend(_runtime_spec_errors(runtime_spec, expected_sample_id))
            errors.extend(_runtime_contract_errors(result_dir, expected_sample_id))

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
    candidate_spec_path = result_dir / "candidate_assertions.json"
    if candidate_spec_path.is_file():
        candidate_spec = _load_json(candidate_spec_path, errors)
        try:
            validate_frozen_spec(candidate_spec)
        except ValueError as exc:
            errors.append(f"candidate_assertions.json is invalid: {exc}")
        expected_hash = assertion_content_hash(candidate_spec)
    else:
        expected_hash = str(verified.get("content_hash") or "")
    if assertion_results.get("candidate_content_hash") != expected_hash:
        errors.append("assertion_results candidate_content_hash does not match candidate spec")
    if str(verified.get("content_hash") or "") != expected_hash:
        errors.append("verified_assertions content_hash does not match candidate spec")
    root_verified = assertion_results.get("required_verified")
    if root_verified is None:
        root_verified = assertion_results.get("all_verified")
    if root_verified is not True:
        errors.append("assertion_results.json required_verified is not true")
    if not any(
        isinstance(assertion, dict) and assertion.get("kind") == "required"
        for assertion in verified.get("assertions", [])
    ):
        errors.append(
            "verified_assertions.json has no required root-obligation assertion"
        )
    errors.extend(_contract_errors(documents))
    field_bindings = _load_json(result_dir / "field_bindings.json", errors)
    event_locations = _load_json(result_dir / "event_locations.json", errors)
    if documents.get("assertion_reward_spec.json") is not None:
        warnings.append(
            "assertion_reward_spec.json is an optional reward-framework projection "
            "and is not part of the GT package contract"
        )

    binding = validate_invariant_bindings(
        documents["verified_invariants.json"],
        candidate_spec if candidate_spec_path.is_file() else {
            "schema_version": "assertion-spec-v3",
            "sample_id": verified.get("sample_id"),
            "original_case": assertion_results.get("original_case", "original"),
            "assertions": verified.get("assertions", []),
            "content_hash": expected_hash,
        },
    )
    errors.extend(f"invariant binding: {message}" for message in binding["errors"])
    errors.extend(_verified_invariant_harness_errors(documents["verified_invariants.json"]))

    perturbations = documents["perturbation_results.json"]
    if (
        perturbations.get("all_needed_witnessed") is not True
        and perturbations.get("accepted_after_single_attempt") is not True
    ):
        errors.append("perturbation_results.json has no accepted required perturbation evidence")

    reachability = documents["reachability_report.json"]
    if (result_dir / "reachability_report.json").is_file() and not _gt_generation_reachability_gate(reachability):
        errors.append(
            "reachability_report.json does not satisfy GT generation gate "
            "(R1/R2/R3/R5 plus sink line or verified sink assertion event)"
        )
    if (result_dir / "reachability_report.json").is_file():
        errors.extend(_artifact_reference_errors(reachability, result_dir))

    commitment_path = result_dir / "evidence_commitment.json"
    provenance_path = result_dir / "generation_provenance.json"
    commitment_required = False
    if provenance_path.is_file():
        provenance = _load_json(provenance_path, errors)
        commitment_required = provenance.get("evidence_commitment_required") is True
    if commitment_required and not commitment_path.is_file():
        errors.append("missing required file: evidence_commitment.json")
    if commitment_path.is_file():
        commitment = _load_json(commitment_path, errors)
        if str(commitment.get("sample_id") or "") != expected_sample_id:
            errors.append("evidence_commitment.json sample_id does not match package")
        errors.extend(commitment_errors(result_dir, commitment))

    context_trace = documents.get("context_trace.json")
    if context_trace is not None:
        errors.extend(context_trace_errors(result_dir, context_trace))

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


def _runtime_archive_errors(result_dir: Path, sample_id: str) -> list[str]:
    errors: list[str] = []
    manifest_path = result_dir / "runtime_work_manifest.json"
    if not manifest_path.is_file():
        errors.append("missing required file for non-ARVO sample: runtime_work_manifest.json")
        return errors
    manifest = _load_json(manifest_path, errors)
    if manifest.get("sample_id") != sample_id:
        errors.append(
            f"runtime_work_manifest.json sample_id mismatch: {manifest.get('sample_id')!r} != {sample_id!r}"
        )
    archive_name = str(manifest.get("archive") or "")
    if archive_name not in RUNTIME_ARCHIVE_NAMES:
        errors.append("runtime_work_manifest.json archive does not match packaged archive")
    archive = result_dir / archive_name if archive_name else result_dir / "runtime_work.tar.gz"
    parts = manifest.get("parts")
    if archive.is_file() and parts:
        errors.append("runtime package cannot contain both full archive and split parts")
    if archive.is_file():
        if int(manifest.get("bytes") or -1) != archive.stat().st_size:
            errors.append("runtime_work_manifest.json bytes does not match packaged archive")
        actual_sha = _sha256_file(archive)
    elif isinstance(parts, list) and parts:
        actual_bytes = 0
        digest = hashlib.sha256()
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                errors.append("runtime_work_manifest.json part entries must be objects")
                continue
            name = str(part.get("name") or "")
            if not name.startswith(f"{archive_name}.part-"):
                errors.append(f"runtime archive part has unexpected name: {name}")
                continue
            path = result_dir / name
            if not path.is_file():
                errors.append(f"runtime archive part is missing: {name}")
                continue
            expected_size = int(part.get("bytes") or -1)
            if expected_size != path.stat().st_size:
                errors.append(f"runtime archive part bytes mismatch: {name}")
            expected_part_sha = str(part.get("sha256") or "")
            actual_part_sha = _sha256_file(path)
            if expected_part_sha != actual_part_sha:
                errors.append(f"runtime archive part sha256 mismatch: {name}")
            actual_bytes += path.stat().st_size
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            expected_index = f"{archive_name}.part-{index:03d}"
            if name != expected_index:
                errors.append(f"runtime archive part order mismatch: {name} != {expected_index}")
        if int(manifest.get("bytes") or -1) != actual_bytes:
            errors.append("runtime_work_manifest.json bytes does not match split archive bytes")
        actual_sha = digest.hexdigest()
    else:
        errors.append("missing required file for non-ARVO sample: runtime_work archive")
        return errors
    expected_sha = str(manifest.get("sha256") or "")
    if expected_sha:
        if actual_sha != expected_sha:
            errors.append("runtime_work_manifest.json sha256 does not match packaged archive")
    else:
        errors.append("runtime_work_manifest.json missing sha256")
    return errors


def _runtime_contract_errors(result_dir: Path, sample_id: str) -> list[str]:
    has_archive_manifest = (result_dir / "runtime_work_manifest.json").is_file()
    has_build_recipe = (result_dir / RUNTIME_BUILD_RECIPE_NAME).is_file()
    if has_build_recipe:
        return _runtime_build_recipe_errors(result_dir, sample_id)
    if has_archive_manifest:
        return _runtime_archive_errors(result_dir, sample_id)
    return [
        "missing non-ARVO runtime contract: provide runtime_build.json "
        "for rebuildable samples or runtime_work_manifest.json plus archive "
        "for non-rebuildable samples"
    ]


def _runtime_build_recipe_errors(result_dir: Path, sample_id: str) -> list[str]:
    errors: list[str] = []
    recipe = _load_json(result_dir / RUNTIME_BUILD_RECIPE_NAME, errors)
    if recipe.get("schema_version") != "gt-runtime-build-v1":
        errors.append("runtime_build.json schema_version must be gt-runtime-build-v1")
    if recipe.get("sample_id") != sample_id:
        errors.append(
            f"runtime_build.json sample_id mismatch: {recipe.get('sample_id')!r} != {sample_id!r}"
        )
    commands = recipe.get("commands")
    if not isinstance(commands, list) or not commands:
        errors.append("runtime_build.json commands must be a non-empty array")
        return errors
    for index, item in enumerate(commands):
        if not isinstance(item, dict):
            errors.append(f"runtime_build.json commands[{index}] must be an object")
            continue
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            errors.append(f"runtime_build.json commands[{index}].command must be non-empty")
        elif "\x00" in command:
            errors.append(f"runtime_build.json commands[{index}].command contains NUL")
        source = item.get("source")
        if source is not None and not isinstance(source, str):
            errors.append(f"runtime_build.json commands[{index}].source must be a string")
        if "run_as_root" in item and not isinstance(item.get("run_as_root"), bool):
            errors.append(f"runtime_build.json commands[{index}].run_as_root must be boolean")
        environment = item.get("environment", {})
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        ):
            errors.append(f"runtime_build.json commands[{index}].environment must be an object of strings")
    portability_path = result_dir / "portability_report.json"
    materials_path = result_dir / "runtime_materials.json"
    if not portability_path.is_file():
        errors.append("missing non-ARVO Stage 01 portability_report.json")
    else:
        portability = _load_json(portability_path, errors)
        if portability.get("schema_version") != "gt-stage01-portability-v1":
            errors.append("portability_report.json has unsupported schema_version")
        if portability.get("sample_id") != sample_id:
            errors.append("portability_report.json sample_id mismatch")
        if portability.get("runtime_portable") is not True:
            errors.append("portability_report.json runtime_portable must be true")
        if portability.get("clean_replay_ok") is not True:
            errors.append("portability_report.json clean_replay_ok must be true")
    if not materials_path.is_file():
        errors.append("missing non-ARVO runtime_materials.json")
    else:
        materials = _load_json(materials_path, errors)
        if materials.get("schema_version") != "gt-runtime-materials-v1":
            errors.append("runtime_materials.json has unsupported schema_version")
        if materials.get("sample_id") != sample_id:
            errors.append("runtime_materials.json sample_id mismatch")
        entries = materials.get("files")
        if not isinstance(entries, list) or not entries:
            errors.append("runtime_materials.json files must be a non-empty array")
        else:
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    errors.append(f"runtime_materials.json files[{index}] must be an object")
                    continue
                relative = str(entry.get("path") or "")
                path = result_dir / relative
                try:
                    path.resolve().relative_to(result_dir.resolve())
                except ValueError:
                    errors.append(f"runtime_materials.json path escapes package: {relative}")
                    continue
                if not path.is_file():
                    errors.append(f"runtime material is missing: {relative}")
                elif entry.get("sha256") != "sha256:" + _sha256_file(path):
                    errors.append(f"runtime material hash mismatch: {relative}")
    return errors


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
