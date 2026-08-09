import json

from gt_generation.gt_toolkit import evidence


def _write_bundle(root):
    for name in evidence.COMMITMENT_FILES:
        (root / name).write_text(
            json.dumps({"sample_id": "sample", "name": name}),
            encoding="utf-8",
        )


def test_commitment_detects_any_changed_evidence_file(tmp_path):
    _write_bundle(tmp_path)
    commitment = evidence.write_commitment(tmp_path)

    assert evidence.commitment_errors(tmp_path, commitment) == []

    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"sample_id": "sample", "changed": True}),
        encoding="utf-8",
    )

    assert evidence.commitment_errors(tmp_path, commitment) == [
        "committed evidence hash does not match ground_truth.json"
    ]


def test_commitment_requires_complete_bundle(tmp_path):
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"sample_id": "sample"}),
        encoding="utf-8",
    )

    try:
        evidence.build_commitment(tmp_path)
    except ValueError as exc:
        assert "cannot bind incomplete evidence bundle" in str(exc)
    else:
        raise AssertionError("incomplete evidence bundle was accepted")
