import json

from evaluator.reasoning.fine_trace_coverage import score_fine_trace_coverage


def _gt(tmp_path, payload):
    gt_dir = tmp_path / "sample"
    gt_dir.mkdir(exist_ok=True)
    (gt_dir / "ground_truth.json").write_text(json.dumps(payload), encoding="utf-8")
    return gt_dir


GT = {
    "sample_id": "sample",
    "fine_trace": [
        {"step": 1, "file": "src/a.c", "function": "parse", "line": 10, "var": "len", "code": "len = input[0];"},
        {"step": 2, "file": "src/a.c", "function": "carry", "line": 15, "var": "n", "code": "n = len;",
         "depends_on": [{"on": 1, "type": "data", "via": "len"}]},
        {"step": 3, "file": "src/a.c", "function": "bug", "line": 20, "var": "buf[n]", "code": "buf[n] = 0;",
         "depends_on": [{"on": 2, "type": "data", "via": "n"}]},
    ],
    "reachability_checkpoints": {"parser_admitted": {"admitted_location": {"file": "src/a.c", "function": "parse", "line": 11}}},
    "source": {"file": "src/a.c", "function": "parse", "line": 10},
    "root_cause": {"file": "src/a.c", "function": "carry", "line": 15},
    "sink": {"file": "src/a.c", "function": "bug", "line": 20},
    "sanitizer_ground_truth": {"crash_location": {"file": "src/a.c", "function": "bug", "line": 20}},
}


def test_full_recall_when_every_node_and_edge_is_recovered(tmp_path):
    analysis = {"sample_id": "sample", "fine_trace": [
        {"step": 1, "file": "/workspace/repo-vul/src-vul/src/a.c", "function": "parse", "line": 10, "role": "source"},
        {"step": 2, "file": "src/a.c", "function": "carry", "line": 15},
        {"step": 3, "file": "src/a.c", "function": "bug", "line": 20, "role": "sink"},
    ]}
    result = score_fine_trace_coverage("sample", analysis, gt_dir=_gt(tmp_path, GT))
    assert result["nodes"] == {"total": 3, "covered": 3, "recall": 1.0}
    assert result["edges"] == {"total": 2, "covered": 2, "recall": 1.0}
    assert result["stage_coverage"]["source"] is True
    assert result["stage_coverage"]["sink"] is True


def test_edges_need_both_endpoints_and_causal_order(tmp_path):
    # Both endpoints of edge 2->3 are present, but stated in reverse order.
    analysis = {"sample_id": "sample", "fine_trace": [
        {"step": 1, "file": "src/a.c", "function": "bug", "line": 20},
        {"step": 2, "file": "src/a.c", "function": "carry", "line": 15},
        {"step": 3, "file": "src/a.c", "function": "parse", "line": 10},
    ]}
    result = score_fine_trace_coverage("sample", analysis, gt_dir=_gt(tmp_path, GT))
    assert result["nodes"]["covered"] == 3
    assert result["edges"]["covered"] == 0


def test_partial_recall_is_a_plain_fraction(tmp_path):
    analysis = {"sample_id": "sample", "fine_trace": [
        {"step": 1, "file": "src/a.c", "function": "parse", "line": 10},
        {"step": 2, "file": "src/a.c", "function": "bug", "line": 20},
    ]}
    result = score_fine_trace_coverage("sample", analysis, gt_dir=_gt(tmp_path, GT))
    assert result["nodes"]["covered"] == 2
    assert result["nodes"]["recall"] == 2 / 3
    # 1->2 and 2->3 both need node 2, which was not recovered.
    assert result["edges"]["covered"] == 0
    assert result["stage_coverage"]["root_cause"] is False


def test_one_subject_step_cannot_cover_two_gt_nodes(tmp_path):
    gt = json.loads(json.dumps(GT))
    gt["fine_trace"][1] = {"step": 2, "file": "src/a.c", "function": "parse", "line": 12,
                           "depends_on": [{"on": 1, "type": "data", "via": "len"}]}
    analysis = {"sample_id": "sample", "fine_trace": [
        {"step": 1, "file": "src/a.c", "function": "parse", "line": 11},
    ]}
    result = score_fine_trace_coverage("sample", analysis, gt_dir=_gt(tmp_path, gt))
    assert result["nodes"]["covered"] == 1


def test_qualified_cpp_member_names_match_unqualified_subjects(tmp_path):
    gt = {"fine_trace": [{"step": 1, "file": "src/Parser.cc", "function": "Parser::makeStream", "line": 203, "line_end": 206}]}
    analysis = {"fine_trace": [{"step": 1, "file": "src/Parser.cc", "function": "makeStream", "line": 205}]}
    result = score_fine_trace_coverage("sample", analysis, gt_dir=_gt(tmp_path, gt))
    assert result["nodes"]["covered"] == 1


def test_missing_analysis_reports_unavailable(tmp_path):
    result = score_fine_trace_coverage("sample", None, gt_dir=_gt(tmp_path, {"fine_trace": []}))
    assert "unavailable" in result
    assert result["nodes"]["covered"] == 0
    assert result["nodes"]["recall"] is None


def test_differently_qualified_functions_stay_distinct(tmp_path):
    # Same member name under different scopes is a different program point;
    # crediting it would inflate node recall for a trace that found the wrong one.
    gt = {"fine_trace": [{"step": 1, "file": "src/a.cc", "function": "left::Parser::parse", "line": 10}]}
    analysis = {"fine_trace": [{"step": 1, "file": "src/a.cc", "function": "right::Parser::parse", "line": 10}]}
    result = score_fine_trace_coverage("sample", analysis, gt_dir=_gt(tmp_path, gt))
    assert result["nodes"]["covered"] == 0
