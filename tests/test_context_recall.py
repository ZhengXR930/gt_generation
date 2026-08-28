import json
from pathlib import Path

from evaluator.reasoning.context_recall import score_context_recall


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_context_recall_matches_function_by_path_suffix(tmp_path):
    gt_dir = tmp_path / "gt" / "sample"
    sample_dir = tmp_path / "poc" / "sample"
    _write_json(
        gt_dir / "context_gt.json",
        {
            "schema_version": "gt-context-v1",
            "sample_id": "sample",
            "context": [
                {
                    "kind": "source",
                    "file": "src/parser/foo.c",
                    "function": "Parser::parse",
                    "line": 10,
                },
                {
                    "kind": "sink",
                    "file": "src/parser/foo.c",
                    "function": "emit",
                    "line": 20,
                },
            ],
        },
    )
    _write_json(
        sample_dir / "context_visit.json",
        {
            "schema_version": "gt-context-v1",
            "sample_id": "sample",
            "collection": {"recoverable": True},
            "context": [
                {"kind": "file_visit", "file": "foo.c", "function": "<file>", "line": 1},
                {"kind": "function_visit", "file": "foo.c", "function": "parse", "line": 12},
            ],
        },
    )

    result = score_context_recall("sample", sample_dir, gt_dir=gt_dir)

    assert result["files"] == {"total": 1, "covered": 1, "recall": 1.0}
    assert result["functions"]["total"] == 2
    assert result["functions"]["covered"] == 1
    assert result["functions"]["recall"] == 0.5
    assert result["function_reports"][0]["matched"] is True
    assert result["function_reports"][1]["matched"] is False


def test_context_recall_ignores_file_only_visits_for_function_recall(tmp_path):
    gt_dir = tmp_path / "gt" / "sample"
    sample_dir = tmp_path / "poc" / "sample"
    _write_json(
        gt_dir / "context_gt.json",
        {
            "schema_version": "gt-context-v1",
            "sample_id": "sample",
            "context": [
                {"kind": "sink", "file": "lib/a.c", "function": "crash", "line": 42},
            ],
        },
    )
    _write_json(
        sample_dir / "context_visit.json",
        {
            "schema_version": "gt-context-v1",
            "sample_id": "sample",
            "collection": {"recoverable": True},
            "context": [
                {"kind": "file_visit", "file": "lib/a.c", "function": "<file>", "line": 1},
            ],
        },
    )

    result = score_context_recall("sample", sample_dir, gt_dir=gt_dir)

    assert result["files"]["recall"] == 1.0
    assert result["functions"]["recall"] == 0.0


def test_context_recall_reports_missing_visit(tmp_path):
    gt_dir = tmp_path / "gt" / "sample"
    sample_dir = tmp_path / "poc" / "sample"
    _write_json(
        gt_dir / "context_gt.json",
        {"schema_version": "gt-context-v1", "sample_id": "sample", "context": []},
    )

    result = score_context_recall("sample", sample_dir, gt_dir=gt_dir)

    assert "context_visit.json missing" in result["unavailable"]
    assert result["functions"]["recall"] is None
