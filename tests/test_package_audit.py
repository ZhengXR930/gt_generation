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


def test_package_audit_accepts_standalone_public_crash_trace(tmp_path):
    (tmp_path / "default_crash_trace.txt").write_text("ASAN crash state")

    assert package_audit._has_default_crash_trace({}, tmp_path) is True


def test_package_audit_accepts_string_issue_description():
    assert package_audit._public_issue({"issue_description": "exact issue"}) == "exact issue"


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


def test_package_audit_rejects_legacy_assertions_even_when_legacy_checks_pass(
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
    assert any("must use verified-assertions-v3" in error for error in report["errors"])
