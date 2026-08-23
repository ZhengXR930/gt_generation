import json

from gt_generation.gt_toolkit import compact_result


def test_compact_result_keeps_only_durable_files(tmp_path, monkeypatch):
    for name in compact_result.KEEP_FILES:
        path = tmp_path / name
        if name == "reachability_report.json":
            path.write_text(json.dumps({"sample_id": "sample", "artifacts": {"raw": "raw.txt"}}))
        else:
            path.write_text("{}" if name.endswith(".json") else "asset")
    (tmp_path / "candidate_assertions.json").write_text("{}")
    (tmp_path / "vulnerable_assertion_trace.txt").write_text("trace")
    (tmp_path / "role_logs").mkdir()
    (tmp_path / "role_logs" / "stage.log").write_text("log")
    (tmp_path / "runtime_work.tar.gz").write_bytes(b"stale archive")
    (tmp_path / "oss_fuzz_src").mkdir()
    (tmp_path / "oss_fuzz_src" / "unused-helper").write_text("unused")

    monkeypatch.setattr(
        compact_result,
        "audit_package",
        lambda result_dir: {
            "sample_id": "sample",
            "ok": True,
            "errors": [],
            "warnings": [],
        },
    )

    report = compact_result.compact_result(tmp_path)

    assert report["ok"] is True
    assert {path.name for path in tmp_path.iterdir()} == compact_result.KEEP_FILES
    reachability = json.loads((tmp_path / "reachability_report.json").read_text())
    assert "artifacts" not in reachability
