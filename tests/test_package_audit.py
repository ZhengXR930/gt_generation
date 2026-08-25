import json
from types import SimpleNamespace

from gt_generation.gt_toolkit import package_audit
from gt_generation.gt_toolkit.assertions import assertion_content_hash


def test_package_audit_rejects_missing_and_escaping_evidence_paths(tmp_path):
    report = {
        "artifacts": {
            "missing": "reachability/missing.json",
            "escape": "../outside.txt",
        }
    }

    errors = package_audit._artifact_reference_errors(report, tmp_path)

    assert any("is missing" in error for error in errors)
    assert any("escapes result directory" in error for error in errors)


def test_verified_invariant_harness_errors_reject_scored_edges():
    errors = package_audit._verified_invariant_harness_errors(
        {
            "root_cause_criterion": {
                "invariant_id": "root",
                "file": "src/parser.c",
                "function": "parse",
                "line": 10,
            },
            "nodes": [],
            "edges": [
                {
                    "invariant_id": "edge.harness",
                    "from_file": "src/parser.c",
                    "from_function": "parse",
                    "from_line": 10,
                    "to_file": "tests/fuzz/parser_fuzzer.c",
                    "to_function": "LLVMFuzzerTestOneInput",
                    "to_line": 40,
                }
            ],
        }
    )

    assert any("edges[0].to is anchored in unscored fuzzing harness" in error for error in errors)


def test_package_audit_accepts_standalone_public_crash_trace(tmp_path):
    (tmp_path / "default_crash_trace.txt").write_text("ASAN crash state")

    assert package_audit._has_default_crash_trace({}, tmp_path) is True


def test_package_audit_accepts_string_issue_description():
    assert package_audit._public_issue({"issue_description": "exact issue"}) == "exact issue"


def test_package_audit_accepts_runtime_build_contract_without_archive(tmp_path):
    sample_id = "secbench_case"
    (tmp_path / "runtime_build.json").write_text(json.dumps({
        "schema_version": "gt-runtime-build-v1",
        "sample_id": sample_id,
        "commands": [{
            "source": "oss_fuzz_staged_recipe",
            "command": "source /gt/oss_fuzz_setup.sh && bash /gt/oss_fuzz_build.sh",
            "run_as_root": True,
            "environment": {"GT_BUILD_JOBS": "1"},
        }],
    }))
    (tmp_path / "portability_report.json").write_text(json.dumps({
        "schema_version": "gt-stage01-portability-v1",
        "sample_id": sample_id,
        "runtime_portable": True,
        "clean_replay_ok": True,
    }))
    build_sha = package_audit._sha256_file(tmp_path / "runtime_build.json")
    (tmp_path / "runtime_materials.json").write_text(json.dumps({
        "schema_version": "gt-runtime-materials-v1",
        "sample_id": sample_id,
        "files": [{
            "path": "runtime_build.json",
            "sha256": "sha256:" + build_sha,
        }],
    }))

    errors = package_audit._runtime_contract_errors(tmp_path, sample_id)

    assert errors == []


def test_package_audit_rejects_non_arvo_without_runtime_contract(tmp_path):
    errors = package_audit._runtime_contract_errors(tmp_path, "secbench_case")

    assert errors == [
        "missing non-ARVO runtime contract: provide runtime_build.json "
        "for rebuildable samples or runtime_work_manifest.json plus archive "
        "for non-rebuildable samples"
    ]


def test_package_audit_requires_root_obligation_assertion(tmp_path, monkeypatch):
    sample_id = "sample"
    assertion = {
        "id": "observed.node",
        "kind": "observed",
        "at": "point",
        "check": ["eq", "$left", "$right"],
        "invariants": ["node.one"],
    }
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": sample_id,
        "original_case": "original",
        "assertions": [assertion],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    documents = {
        "sample_info.json": {
            "sample_id": sample_id,
            "original_bug_description": "exact public issue",
            "default_crash_trace": "exact public crash trace",
        },
        "ground_truth.json": {"sample_id": sample_id},
        "verified_invariants.json": {
            "sample_id": sample_id,
            "nodes": [{"invariant_id": "node.one", "verified": True}],
            "edges": [],
        },
        "verified_assertions.json": {
            "schema_version": "verified-assertions-v3",
            "sample_id": sample_id,
            "content_hash": spec["content_hash"],
            "assertions": [assertion],
        },
        "assertion_results.json": {
            "sample_id": sample_id,
            "original_case": "original",
            "candidate_content_hash": spec["content_hash"],
            "all_verified": True,
        },
        "perturbation_results.json": {
            "sample_id": sample_id,
            "all_needed_witnessed": True,
        },
        "field_bindings.json": {"sample_id": sample_id},
        "event_locations.json": {"sample_id": sample_id},
        "reachability_report.json": {
            "sample_id": sample_id,
            **{field: True for field in package_audit.REACHABILITY_FIELDS},
            "artifacts": {},
        },
    }
    for name in package_audit.REQUIRED_FILES:
        path = tmp_path / name
        path.write_text(json.dumps(documents[name]) if name.endswith(".json") else "asset")
    monkeypatch.setattr(
        package_audit,
        "validate_data",
        lambda *args, **kwargs: SimpleNamespace(errors=[], warnings=[]),
    )

    report = package_audit.audit_package(tmp_path)

    assert report["ok"] is False
    assert any("no required root-obligation" in error for error in report["errors"])


def test_package_audit_rejects_artifact_level_schema_version_even_when_legacy_checks_pass(
    tmp_path, monkeypatch
):
    sample_id = "sample"
    assertion = {
        "id": "observed.node",
        "kind": "observed",
        "at": "point",
        "check": ["eq", "$left", "$right"],
        "invariants": ["node.one"],
    }
    spec = {
        "schema_version": "assertion-spec-v2",
        "sample_id": sample_id,
        "original_case": "original",
        "assertions": [assertion],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    documents = {
        "sample_info.json": {
            "sample_id": sample_id,
            "original_bug_description": "exact public issue",
            "default_crash_trace": "exact public crash trace",
        },
        "ground_truth.json": {"sample_id": sample_id},
        "verified_invariants.json": {
            "sample_id": sample_id,
            "nodes": [{"invariant_id": "node.one", "verified": True}],
            "edges": [],
        },
        "verified_assertions.json": {
            "schema_version": "verified-assertions-v2",
            "sample_id": sample_id,
            "content_hash": spec["content_hash"],
            "assertions": [assertion],
        },
        "assertion_results.json": {
            "sample_id": sample_id,
            "original_case": "original",
            "candidate_content_hash": spec["content_hash"],
            "all_verified": True,
        },
        "perturbation_results.json": {
            "sample_id": sample_id,
            "all_needed_witnessed": True,
        },
        "field_bindings.json": {"sample_id": sample_id},
        "event_locations.json": {"sample_id": sample_id},
        "reachability_report.json": {
            "sample_id": sample_id,
            **{field: True for field in package_audit.REACHABILITY_FIELDS},
            "artifacts": {},
        },
    }
    for name in package_audit.REQUIRED_FILES:
        path = tmp_path / name
        if name.endswith(".json"):
            path.write_text(json.dumps(documents[name]))
        else:
            path.write_text("asset")
    monkeypatch.setattr(
        package_audit,
        "validate_data",
        lambda *args, **kwargs: SimpleNamespace(errors=[], warnings=[]),
    )

    report = package_audit.audit_package(tmp_path)

    assert report["ok"] is False
    assert any(
        "verified_assertions.json must not contain artifact-level schema_version" in error
        for error in report["errors"]
    )


def test_package_audit_validates_context_trace_when_present(tmp_path, monkeypatch):
    sample_id = "sample"
    assertion = {
        "id": "required.root",
        "kind": "required",
        "at": "point",
        "check": ["eq", "$left", "$right"],
        "invariants": ["node.one"],
    }
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": sample_id,
        "original_case": "original",
        "assertions": [assertion],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    documents = {
        "sample_info.json": {
            "sample_id": sample_id,
            "original_bug_description": "exact public issue",
            "default_crash_trace": "exact public crash trace",
        },
        "ground_truth.json": {"sample_id": sample_id},
        "verified_invariants.json": {
            "sample_id": sample_id,
            "nodes": [{"invariant_id": "node.one", "verified": True}],
            "edges": [],
        },
        "verified_assertions.json": {
            "schema_version": "verified-assertions-v3",
            "sample_id": sample_id,
            "content_hash": spec["content_hash"],
            "assertions": [assertion],
        },
        "assertion_results.json": {
            "sample_id": sample_id,
            "original_case": "original",
            "candidate_content_hash": spec["content_hash"],
            "required_verified": True,
        },
        "perturbation_results.json": {
            "sample_id": sample_id,
            "all_needed_witnessed": True,
        },
        "field_bindings.json": {"sample_id": sample_id},
        "event_locations.json": {"sample_id": sample_id},
        "reachability_report.json": {
            "sample_id": sample_id,
            "reachability_checked": True,
            "target_vulnerability_triggered": True,
            "R2_source_reached": True,
            "R3_root_cause_reached": True,
            "R4_sink_reached": True,
            **{field: True for field in package_audit.REACHABILITY_FIELDS},
            "artifacts": {},
        },
        "context_gt.json": {
            "schema_version": "gt-context-v1",
            "sample_id": sample_id,
            "collection": {},
            "context": [],
        },
    }
    for name in package_audit.REQUIRED_FILES:
        path = tmp_path / name
        path.write_text(json.dumps(documents[name]) if name.endswith(".json") else "asset")
    (tmp_path / "context_gt.json").write_text(json.dumps(documents["context_gt.json"]))
    monkeypatch.setattr(
        package_audit,
        "validate_data",
        lambda *args, **kwargs: SimpleNamespace(errors=[], warnings=[]),
    )

    report = package_audit.audit_package(tmp_path)

    assert report["ok"] is False
    assert "context_gt.json context is empty" in report["errors"]
