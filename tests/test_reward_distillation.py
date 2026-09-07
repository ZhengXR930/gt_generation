import json

import pytest
from pathlib import Path

from reward_framework.distillation.lint import lint_lesson_text, load_literal_index
from reward_framework.distillation.outcome import classify_outcome, diagnostics_summary
from reward_framework.distillation.pools import build_validation_panel, empty_pools, teacher_view, update_pools
from reward_framework.distillation.artifacts import write_json
from reward_framework.distillation.cli import _diagnosis_quality_error, _normalize_diagnosis_for_outcome
from reward_framework.distillation.diagnosis_view import abstract_text, learning_diagnosis, learning_evaluation
from reward_framework.distillation.prompts import load_template, role_task_prompt
from reward_framework.distillation.roles import (
    build_correction_role,
    build_curator_role,
    build_diagnosis_role,
    build_teacher_role,
    plan_role,
    resume_role,
)
from reward_framework.distillation.skill_packet import (
    TARGETS,
    apply_correction_decision,
    apply_curator_decisions,
    copy_initial_packet,
    lessons_snapshot,
    read_lessons,
)
from reward_framework.distillation.splits import build_chronological_split


def _valid_gt(tmp_path, count=500):
    valid = tmp_path / "valid_gt.json"
    valid.write_text(json.dumps({"samples": [f"s{i}" for i in range(count)]}), encoding="utf-8")
    return valid


def _section(summary, evidence="evidence"):
    return {"summary": summary, "evidence": evidence}


def _behavioral_fields(prefix="behavior"):
    return {
        "search_behavior": _section(f"{prefix} search", f"{prefix} search evidence"),
        "candidate_behavior": _section(f"{prefix} candidate", f"{prefix} candidate evidence"),
        "feedback_behavior": _section(f"{prefix} feedback", f"{prefix} feedback evidence"),
        "outcome_diagnosis": _section(f"{prefix} outcome", f"{prefix} outcome evidence"),
        "transferable_observation": _section(f"{prefix} transferable", f"{prefix} transferable evidence"),
    }


def test_split_falls_back_to_corpus_order_without_dates(tmp_path):
    empty = tmp_path / "no_dates.json"
    empty.write_text(json.dumps({"rows": []}), encoding="utf-8")
    split = build_chronological_split(_valid_gt(tmp_path), commit_dates=empty)
    assert split.train[0] == "s0"
    assert split.test[0] == "s300"
    assert len(split.batches) == 30
    # The fallback must announce itself rather than silently claiming chronology.
    assert "not chronological" in split.ordering


def test_split_orders_by_commit_date(tmp_path):
    dates = tmp_path / "commit_dates.json"
    dates.write_text(json.dumps({"rows": [
        {"sample_id": f"s{i}", "vulnerable_commit_date": f"20{25 - i // 100:02d}-01-{(i % 28) + 1:02d}T00:00:00+00:00"}
        for i in range(500)
    ]}), encoding="utf-8")
    split = build_chronological_split(_valid_gt(tmp_path), commit_dates=dates)
    ordered = split.train + split.test
    values = [split.dates[s] for s in ordered if s in split.dates]
    assert values == sorted(values), "split must be ascending in commit date"
    assert split.dates[split.train[-1]] <= split.dates[split.test[0]]
    assert not split.undated


def test_lessons_get_stable_ids_and_capacity(tmp_path):
    packet = copy_initial_packet(tmp_path / "packet")
    snapshot = lessons_snapshot(packet)
    assert set(snapshot) == set(TARGETS)
    assert snapshot["reproduction:R.B"]["lessons"] == []
    assert snapshot["submission:S.C"]["lessons"] == []
    assert snapshot["reproduction:R.B"]["capacity"] == TARGETS["reproduction:R.B"][2]


def test_modify_replaces_in_place_and_skip_writes_nothing(tmp_path):
    packet = copy_initial_packet(tmp_path / "packet")
    seeded = tmp_path / "seeded"
    apply_curator_decisions(packet, [
        {"decision": "MODIFY", "target": "reproduction:R.B",
         "proposal": "Keep container framing valid while changing the smallest admitting field."},
    ], out_packet=seeded)
    out = tmp_path / "next"
    result = apply_curator_decisions(seeded, [
        {"decision": "MODIFY", "target": "reproduction:R.B", "target_lesson_id": "R.B#1",
         "proposal": "Keep the first issue-matching candidate small while changing the smallest admitting field.",
         "evidence": "sample x"},
        {"decision": "MODIFY", "target": "submission:S.C",
         "proposal": "Preserve controlling values when moving from root cause toward the sink.",
         "evidence": "sample y"},
        {"decision": "SKIP", "target": "reproduction:R.A", "proposal": "change fixed loop"},
    ], out_packet=out)

    assert len(result["applied"]) == 2
    modes = {item["mode"] for item in result["applied"]}
    assert modes == {"replace", "append"}

    lessons = read_lessons(out)
    replaced = {item["lesson_id"]: item["text"] for item in lessons["reproduction:R.B"]}
    assert replaced["R.B#1"].startswith("Keep the first issue-matching candidate")
    assert len(lessons["reproduction:R.B"]) == 1, "replace must not grow the section"
    assert len(lessons["submission:S.C"]) == 1

    text = (out / "reproduction_skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "change fixed loop" not in text


def test_fixed_sections_are_not_writable():
    for target in ("reproduction:R.A", "reproduction:R.C", "submission:S.A", "submission:S.B"):
        assert target not in TARGETS


def test_evidence_never_reaches_the_packet(tmp_path):
    packet = copy_initial_packet(tmp_path / "packet")
    out = tmp_path / "next"
    apply_curator_decisions(packet, [
        {"decision": "MODIFY", "target": "submission:S.C",
         "proposal": "Distinguish parser failure from sink miss when reading a non-triggering result.",
         "evidence": "SENTINEL_EVIDENCE", "rationale": "SENTINEL_RATIONALE"},
    ], out_packet=out)
    shipped = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in out.rglob("*")
        if path.is_file() and path.suffix in {".md", ".json", ".txt"}
    )
    assert "SENTINEL_EVIDENCE" not in shipped
    assert "SENTINEL_RATIONALE" not in shipped



def test_correction_limits_modify_remove_to_applied_lessons(tmp_path):
    base = copy_initial_packet(tmp_path / "base")
    current = tmp_path / "current"
    apply_curator_decisions(base, [
        {"decision": "MODIFY", "target": "reproduction:R.B",
         "proposal": "Use the strongest issue-aligned feedback as the next starting point."},
        {"decision": "MODIFY", "target": "submission:S.C",
         "proposal": "Treat nearby crashes as evidence before finalizing."},
    ], out_packet=current)

    result = apply_correction_decision(current, base, {
        "decision": "REMOVE",
        "operations": [{"action": "REMOVE", "target": "submission:S.C", "target_lesson_id": "S.C#1"}],
    }, out_packet=tmp_path / "blocked", allowed_lesson_ids={("reproduction:R.B", "R.B#1")})
    assert not result["applied"]
    assert "not introduced or modified" in result["skipped"][0]["skip_reason"]
    assert len(read_lessons(tmp_path / "blocked")["submission:S.C"]) == 1


def test_correction_can_remove_or_rollback_lessons(tmp_path):
    base = copy_initial_packet(tmp_path / "base")
    current = tmp_path / "current"
    apply_curator_decisions(base, [
        {"decision": "MODIFY", "target": "reproduction:R.B",
         "proposal": "Use the strongest issue-aligned feedback as the next starting point."},
        {"decision": "MODIFY", "target": "submission:S.C",
         "proposal": "Treat nearby crashes as evidence before finalizing."},
    ], out_packet=current)

    removed = apply_correction_decision(current, base, {
        "decision": "REMOVE",
        "operations": [{"action": "REMOVE", "target": "reproduction:R.B", "target_lesson_id": "R.B#1"}],
    }, out_packet=tmp_path / "removed")
    assert removed["applied"][0]["mode"] == "remove"
    lessons = read_lessons(tmp_path / "removed")
    assert lessons["reproduction:R.B"] == []
    assert len(lessons["submission:S.C"]) == 1

    rolled_back = apply_correction_decision(current, base, {"decision": "ROLLBACK"},
                                            out_packet=tmp_path / "rollback")
    assert rolled_back["decision"] == "ROLLBACK"
    assert read_lessons(tmp_path / "rollback") == read_lessons(base)

def test_section_capacity_forces_replacement(tmp_path):
    packet = copy_initial_packet(tmp_path / "packet")
    capacity = TARGETS["reproduction:R.B"][2]
    used = len(read_lessons(packet)["reproduction:R.B"])
    filler = [
        {"decision": "MODIFY", "target": "reproduction:R.B",
         "proposal": f"Cross-stage lesson number {i} about preserving progress between stages."}
        for i in range(capacity - used + 1)
    ]
    result = apply_curator_decisions(packet, filler, out_packet=tmp_path / "next")
    assert len(result["applied"]) == capacity - used
    assert any("at capacity" in str(item.get("skip_reason")) for item in result["skipped"])


def test_packet_lint_blocks_ground_truth_anchors(tmp_path):
    index = load_literal_index()
    assert lint_lesson_text("Set the length at parser.c:120 to 0xdeadbeef.", index)
    assert lint_lesson_text("This reproduces arvo_10129 exactly.", index)
    assert not lint_lesson_text(
        "Preserve container framing while changing the smallest field that reaches the vulnerable branch.",
        index,
    )

    packet = copy_initial_packet(tmp_path / "packet")
    result = apply_curator_decisions(packet, [
        {"decision": "MODIFY", "target": "reproduction:R.B",
         "proposal": "Set the header size at parser.c:3423 to 65535."},
    ], out_packet=tmp_path / "next")
    assert not result["applied"]
    assert result["skipped"][0]["skip_reason"] == "blocked by packet lint"


def test_outcome_is_deterministic_from_reachability():
    triggered = classify_outcome({"runtime": {"submitted_unique_pocs": 1, "candidates": [
        {"execution_status": "executed", "target_vulnerability_triggered": True}]}})
    assert triggered["outcome"] == "success"

    partial = classify_outcome({"runtime": {"submitted_unique_pocs": 2, "candidates": [
        {"execution_status": "executed", "location_reachability": {
            "R1_input_admitted": True, "R2_source_reached": True, "R3_root_cause_reached": False}}]}})
    assert partial["outcome"] == "partial"
    assert partial["first_failed_stage"] == "Root Cause"
    assert partial["deepest_stage"] == "Source"

    nothing = classify_outcome({"runtime": {"submitted_unique_pocs": 0, "candidates": [],
                                            "unavailable": "not applicable: no submitted PoC"}})
    assert nothing["outcome"] == "failure"

    broken = classify_outcome({"runtime": {"submitted_unique_pocs": 3, "candidates": [],
                                           "unavailable": "reachability has not been executed"}})
    assert broken["outcome"] == "infrastructure"


def test_pools_keep_sample_level_records_without_failure_mode_gate():
    pools = empty_pools()
    update_pools(pools, 0, [
        {
            "sample_id": "a",
            "project": "p1",
            "vulnerability_type": "oob read",
            **_behavioral_fields("partial"),
            "outcome_record": {"outcome": "partial", "first_failed_stage": "Sink"},
        },
        {
            "sample_id": "b",
            "project": "p2",
            "vulnerability_type": "uaf",
            **_behavioral_fields("success"),
            "outcome_record": {"outcome": "success", "deepest_stage": "Trigger"},
        },
        {
            "sample_id": "c",
            "project": "p3",
            "vulnerability_type": "x",
            **_behavioral_fields("infra"),
            "outcome_record": {"outcome": "infrastructure"},
        },
    ])
    view = teacher_view(pools)
    assert view["protocol"] == "teacher-pools-view-v5"
    assert "eligible_failure_modes" not in view
    assert "failure_modes" not in view
    assert view["counts"] == {
        "failure_pool": 1,
        "success_pool": 1,
        "crash_pool": 0,
        "infrastructure_pool": 1,
        "batches_seen": 1,
    }
    assert view["failure_pool"][0]["sample_id"] == "a"
    assert view["failure_pool"][0]["search_behavior"]["summary"] == "partial search"
    assert view["crash_pool"] == []
    assert view["success_pool"][0]["sample_id"] == "b"
    assert view["infrastructure_pool"][0]["sample_id"] == "c"


def test_success_pool_keeps_sample_level_diagnosis_records():
    pools = empty_pools()
    update_pools(pools, 0, [{
        "sample_id": "ok1",
        "project": "p1",
        "vulnerability_type": "uaf",
        "outcome": "success",
        **_behavioral_fields("success"),
        "outcome_record": {"outcome": "success", "deepest_stage": "Trigger"},
    }])
    view = teacher_view(pools)
    assert view["success_pool"] == [{
        "sample_id": "ok1",
        "project": "p1",
        "outcome": "success",
        "vulnerability_type": "uaf",
        **_behavioral_fields("success"),
    }]

def test_success_diagnosis_outcome_is_normalized_without_failure_type():
    diagnosis = {
        "sample_id": "ok1",
        "outcome": "partial",
        "failure_type": "reasoning",
        "issue_description": "issue",
        **_behavioral_fields("success"),
    }
    normalized = _normalize_diagnosis_for_outcome(
        diagnosis, {"outcome": "success", "deepest_stage": "Trigger"}
    )
    assert normalized["outcome"] == "success"
    assert "failure_type" not in normalized

    infra = _normalize_diagnosis_for_outcome(
        {"sample_id": "bad", "failure_type": "mixed"}, {"outcome": "infrastructure"}
    )
    assert infra["outcome"] == "infrastructure"
    assert "failure_type" not in infra
    assert "search_behavior" not in infra, "legacy fields must not be synthesized"


def test_learning_diagnosis_strips_evaluator_and_code_coordinates():
    raw = {
        "sample_id": "arvo_1",
        "outcome": "partial",
        "issue_description": "public issue",
        "vulnerability_type": "oob",
        "search_behavior": _section(
            "Reached R4 Sink in `src/foo.c:123` and function parse_input before Trigger failed.",
            "R2_source_reached was true in repo-vul/src-vul/lib/a.cc:77.",
        ),
        "candidate_behavior": _section("manual construction", "See https://example.test and commit abcdef1234567890 for line 55."),
        "feedback_behavior": _section("Parser feedback was reused", "Source path stayed stable."),
        "outcome_diagnosis": _section("Sink reached, Trigger missed", "Root Cause label was incomplete."),
        "transferable_observation": _section("candidate mutation helped", "source artifact evidence"),
    }
    learned = learning_diagnosis(raw, {"outcome": "partial"})
    rendered = json.dumps(learned)
    assert "R4" not in rendered and "R2_source_reached" not in rendered
    assert "Parser" not in rendered and "Source" not in rendered and "Sink" not in rendered and "Trigger" not in rendered
    assert "src/foo.c" not in rendered and "a.cc" not in rendered and "abcdef" not in rendered
    assert "observable failure" in rendered
    assert learned["sample_id"] == "arvo_1"
    assert learned["search_behavior"]["summary"]

def test_learning_evaluation_hides_stage_labels():
    view = learning_evaluation({"batch_index": 1, "rows": [{
        "sample_id": "s",
        "deterministic_outcome": {
            "outcome": "partial",
            "triggered": False,
            "deepest_stage": "Sink",
            "first_failed_stage": "Trigger",
            "submitted_unique_pocs": 2,
            "false_positive": True,
            "false_positive_pocs": 1,
        },
    }]})
    rendered = json.dumps(view)
    assert "deepest_stage" not in rendered
    assert "first_failed_stage" not in rendered
    assert "Sink" not in rendered and "Trigger" not in rendered
    assert view["rows"][0]["progress_summary"] == "candidate produced crash evidence that did not match the target issue"

def test_distillation_role_prompts_are_external_templates():
    expected = {"diagnostician.md", "teacher.md", "curator.md", "correction.md", "README.md"}
    template_dir = Path("reward_framework/distillation/prompt_templates")
    assert expected <= {path.name for path in template_dir.glob("*.md")}
    assert "You are the Sample Behavior Diagnostician." in load_template("diagnostician.md")
    assert "You are the Batch Skill Evolution Teacher." in load_template("teacher.md")
    assert "You are the Skill Update Curator." in load_template("curator.md")
    assert "You are the Skill Correction Agent." in load_template("correction.md")


def test_role_task_prompt_points_at_files_not_inline_payloads():
    prompt = role_task_prompt("teacher.md", inputs=[("inputs/pools.json", "the pools")])
    assert "`inputs/pools.json` — the pools" in prompt
    assert "Write exactly one JSON object to `OUTPUT.json`" in prompt
    # Payloads live on disk so a role can read them with tools, untruncated.
    assert "## JSON Payload" not in prompt


def test_teacher_and_curator_workspaces_carry_their_inputs(tmp_path):
    packet = copy_initial_packet(tmp_path / "packet")

    teacher = build_teacher_role(tmp_path / "ws_teacher", packet,
                                 [{"sample_id": "s1"}], teacher_view(empty_pools()))
    assert (teacher.inputs / "skill_packet" / "reproduction_skill" / "SKILL.md").is_file()
    for name in ("lessons_snapshot.json", "batch_diagnoses.json", "pools.json"):
        assert (teacher.inputs / name).is_file()
    assert teacher.output_file.name == "OUTPUT.json"
    assert "recurrence_gate" not in teacher.prompt, "payloads belong in files, not the prompt"
    assert "inputs/pools.json" in teacher.prompt

    curator = build_curator_role(tmp_path / "ws_curator", packet,
                                 [{"target": "reproduction:R.B", "proposal": "lesson"}])
    assert (curator.inputs / "candidate_updates.json").is_file()
    assert "MODIFY" in curator.prompt

    correction = build_correction_role(
        tmp_path / "ws_correction",
        packet,
        packet,
        {"rows": []},
        [],
        {},
        teacher_view(empty_pools()),
    )
    assert (correction.inputs / "previous_skill" / "reproduction_skill" / "SKILL.md").is_file()
    assert (correction.inputs / "current_skill" / "submission_skill" / "SKILL.md").is_file()
    for name in ("previous_lessons_snapshot.json", "current_lessons_snapshot.json",
                 "batch_evaluation.json", "batch_diagnoses.json", "applied_updates.json", "pools.json"):
        assert (correction.inputs / name).is_file()
    assert "ROLLBACK" in correction.prompt


def test_templates_state_the_proposal_evidence_boundary():
    for name in ("teacher.md", "curator.md"):
        text = load_template(name)
        assert "never written into the packet" in text or "never reaches the packet" in text
    assert "do not generate lessons" in load_template("diagnostician.md")


def _pool_record(sample_id, project, summary="sink miss"):
    return {
        "sample_id": sample_id,
        "project": project,
        "vulnerability_type": "oob read",
        **_behavioral_fields(summary),
        "outcome_record": {"outcome": "partial", "first_failed_stage": "Sink"},
    }


def test_folding_a_batch_twice_does_not_duplicate_sample_level_records():
    pools = empty_pools()
    records = [_pool_record("a", "p1")]
    for _ in range(3):
        update_pools(pools, 0, records)
    view = teacher_view(pools)
    assert view["counts"]["failure_pool"] == 1
    assert view["failure_pool"] == [{
        "sample_id": "a",
        "project": "p1",
        "outcome": "partial",
        "vulnerability_type": "oob read",
        **_behavioral_fields("sink miss"),
    }]

    update_pools(pools, 1, [_pool_record("b", "p2", "same sink miss")])
    view = teacher_view(pools)
    assert view["counts"]["failure_pool"] == 2
    assert [item["sample_id"] for item in view["failure_pool"]] == ["a", "b"]


def test_rerunning_a_batch_with_no_results_withdraws_what_it_contributed():
    pools = empty_pools()
    update_pools(pools, 0, [_pool_record("a", "p1", "first")])
    update_pools(pools, 1, [_pool_record("b", "p2", "second")])
    update_pools(pools, 1, [])

    view = teacher_view(pools)
    assert view["counts"]["failure_pool"] == 1
    assert view["failure_pool"][0]["sample_id"] == "a"
    assert view["failure_pool"][0]["search_behavior"]["summary"] == "first search"


def test_a_sample_disappears_when_its_batch_is_withdrawn():
    pools = empty_pools()
    update_pools(pools, 0, [_pool_record("a", "p1")])
    assert teacher_view(pools)["counts"]["failure_pool"] == 1
    update_pools(pools, 0, [])
    assert teacher_view(pools)["counts"]["failure_pool"] == 0


def test_applied_records_name_the_decision_they_came_from(tmp_path):
    # Two decisions on the same section: attribution has to be by identity, not
    # by target, or the second lesson inherits the first one's failure mode.
    packet = copy_initial_packet(tmp_path / "packet")
    decisions = [
        {"decision": "SKIP", "target": "reproduction:R.B", "proposal": ""},
        {"decision": "MODIFY", "target": "reproduction:R.B",
         "proposal": "Preserve the controlling values when moving from root cause toward the sink."},
        {"decision": "MODIFY", "target": "reproduction:R.B",
         "proposal": "Re-check container framing after any change made for a later stage."},
    ]
    result = apply_curator_decisions(packet, decisions, out_packet=tmp_path / "next")

    assert [item["decision_index"] for item in result["applied"]] == [1, 2]
    assert [item["decision_index"] for item in result["skipped"]] == [0]
    for item in result["applied"]:
        assert decisions[item["decision_index"]]["proposal"], "index must point at the real decision"
    assert len({item["lesson_id"] for item in result["applied"]}) == 2


def _role_workspace(tmp_path, status=None):
    workspace = tmp_path / "ws"
    (workspace / "inputs").mkdir(parents=True)
    (workspace / "TASK.md").write_text("ORIGINAL TASK BODY\n", encoding="utf-8")
    if status is not None:
        write_json(workspace / "STATUS.json", {"status": status})
    return workspace


def test_a_sample_with_an_artifact_is_never_rerun(tmp_path):
    workspace = _role_workspace(tmp_path, status="missing_output")
    out = tmp_path / "diagnosis.json"
    write_json(out, {"sample_id": "s1"})
    assert plan_role(workspace, out)[0] == "reuse"


def test_a_session_that_did_not_write_its_artifact_is_resumed(tmp_path):
    # It ran to completion and its notes are still on disk; only the write is missing.
    for status in ("missing_output", "invalid_output"):
        workspace = _role_workspace(tmp_path / status, status=status)
        plan, reason = plan_role(workspace, workspace / "OUTPUT.json")
        assert plan == "resume"
        assert status in reason


def test_a_session_that_failed_on_its_own_terms_starts_over(tmp_path):
    for status in ("agent_failed", "timeout"):
        workspace = _role_workspace(tmp_path / status, status=status)
        assert plan_role(workspace, workspace / "OUTPUT.json")[0] == "fresh"


def test_a_role_with_no_previous_workspace_is_fresh(tmp_path):
    assert plan_role(tmp_path / "missing", tmp_path / "OUTPUT.json")[0] == "fresh"


def test_resume_replays_the_original_task_with_a_corrective_preamble(tmp_path):
    workspace = _role_workspace(tmp_path, status="missing_output")
    run = resume_role("diagnose", workspace, reason="previous session ended as missing_output")
    assert "ORIGINAL TASK BODY" in run.prompt, "the role must see the task it saw before"
    assert "Do not start the" in run.prompt
    assert "missing_output" in run.prompt
    assert run.workspace == workspace, "resuming must not rebuild the workspace"
    assert (workspace / "inputs").is_dir(), "resuming must not wipe the inputs"


_EVAL_ROW = {
    "sample_id": "s1",
    "fine_trace_coverage": {
        "nodes": {"total": 9, "covered": 6, "recall": 6 / 9},
        "edges": {"total": 12, "covered": 7, "recall": 7 / 12},
        "stage_coverage": {"parser": False, "source": False, "root_cause": True, "sink": True, "trigger": True},
        "node_reports": [{"bulk": "x" * 5000}],
    },
    "reasoning": {
        "dimension_scores": {"source": {"loc": 0, "full": 0}, "sink": {"loc": 1, "full": 1}},
        "diagnostics": {"bulk": "y" * 20000},
    },
    "runtime": {
        "submitted_unique_pocs": 1,
        "reachability_executed_candidates": 1,
        "candidates": [{
            "attempt_id": "a1",
            "execution_status": "executed",
            "target_vulnerability_triggered": False,
            "location_reachability": {
                "reachability_depth": "R2", "R1_input_admitted": True, "R2_source_reached": True,
                "R3_root_cause_reached": False, "R4_sink_reached": False, "R5_sanitizer_triggered": False,
                "bulk": "z" * 5000,
            },
        }],
    },
}



def test_diagnosis_quality_requires_behavior_sections():
    valid = {"issue_description": "i", **_behavioral_fields("ok")}
    assert _diagnosis_quality_error(valid) is None

    missing_evidence = {"issue_description": "i", **_behavioral_fields("ok")}
    missing_evidence["candidate_behavior"] = {"summary": "candidate", "evidence": ""}
    assert "candidate_behavior.evidence" in _diagnosis_quality_error(missing_evidence)

    missing_section = {"issue_description": "i", **_behavioral_fields("ok")}
    missing_section.pop("feedback_behavior")
    assert "feedback_behavior" in _diagnosis_quality_error(missing_section)


def test_diagnostics_summary_carries_all_three_and_stays_small():
    summary = diagnostics_summary(_EVAL_ROW)
    assert set(summary) == {"trace_coverage", "reasoning", "reachability"}
    assert summary["trace_coverage"]["node_recall"] == 6 / 9
    assert summary["trace_coverage"]["edge_recall"] == 7 / 12
    assert summary["reasoning"]["dimension_scores"]["sink"]["full"] == 1
    ladder = summary["reachability"]["candidates"][0]["ladder"]
    assert ladder["R2_source_reached"] is True and ladder["R3_root_cause_reached"] is False
    # The point of the summary is to be readable at a glance, not to restate the row.
    assert len(json.dumps(summary)) < 4000


def test_diagnosis_workspace_names_each_diagnostic_separately(tmp_path):
    results = tmp_path / "results"
    (results / "s1").mkdir(parents=True)
    run = build_diagnosis_role(tmp_path / "ws", "s1", results, _EVAL_ROW,
                               {"outcome": "partial", "first_failed_stage": "Root Cause"})

    for relative in ("diagnostics_summary.json",
                     "diagnostics/trace_coverage.json",
                     "diagnostics/reasoning.json",
                     "diagnostics/reachability.json"):
        assert (run.inputs / relative).is_file(), f"missing {relative}"
        assert f"inputs/{relative}" in run.prompt, f"{relative} is not named in the task prompt"

    # Full detail stays available behind each headline.
    coverage = json.loads((run.inputs / "diagnostics/trace_coverage.json").read_text())
    assert coverage["node_reports"], "the per-step detail must still be reachable"


def _dated_gt(tmp_path, dated_count):
    """A corpus where only the first `dated_count` samples have a commit date."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    valid = tmp_path / "valid_gt.json"
    valid.write_text(json.dumps({"samples": [f"s{i}" for i in range(500)]}), encoding="utf-8")
    dates = tmp_path / "commit_dates.json"
    dates.write_text(json.dumps({"rows": [
        {"sample_id": f"s{i}", "vulnerable_commit_date": f"2020-01-01T00:00:{i % 60:02d}+00:00"}
        for i in range(dated_count)
    ]}), encoding="utf-8")
    return valid, dates


def test_undated_samples_land_in_the_held_out_tail(tmp_path):
    # Undated samples are placed in test on purpose; their position there carries
    # no chronological meaning, and the manifest has to say so.
    valid, dates = _dated_gt(tmp_path, 470)
    split = build_chronological_split(valid, commit_dates=dates)
    assert len(split.undated) == 30
    assert all(sample in split.test for sample in split.undated)
    assert all(sample in split.dates for sample in split.train)
    assert "held-out test tail" in split.to_json()["undated_placement"]


def test_require_dates_allows_undated_test_samples_but_not_undated_training(tmp_path):
    valid, dates = _dated_gt(tmp_path, 470)
    split = build_chronological_split(valid, commit_dates=dates, require_dates=True)
    assert len(split.undated) == 30, "undated in the test tail is an accepted placement"

    valid, dates = _dated_gt(tmp_path / "thin", 250)
    with pytest.raises(ValueError, match="training samples have no commit date"):
        build_chronological_split(valid, commit_dates=dates, require_dates=True)


def test_manifest_records_which_date_axis_each_sample_used(tmp_path):
    valid = tmp_path / "valid_gt.json"
    valid.write_text(json.dumps({"samples": [f"s{i}" for i in range(500)]}), encoding="utf-8")
    dates = tmp_path / "commit_dates.json"
    rows = [{"sample_id": f"s{i}", "vulnerable_commit_date": f"2020-01-01T00:00:{i % 60:02d}+00:00"}
            for i in range(400)]
    rows += [{"sample_id": f"s{i}", "patch_commit_date": f"2021-01-01T00:00:{i % 60:02d}+00:00"}
             for i in range(400, 500)]
    dates.write_text(json.dumps({"rows": rows}), encoding="utf-8")

    manifest = build_chronological_split(valid, commit_dates=dates).to_json()
    assert manifest["date_source_counts"] == {"vulnerable_commit_date": 400, "patch_commit_date": 100}
    assert manifest["date_field_priority"][0] == "vulnerable_commit_date"



def test_validation_panel_selects_success_crash_and_failure_samples():
    pools = empty_pools()
    update_pools(pools, 0, [
        {
            "sample_id": "ok",
            "project": "p1",
            **_behavioral_fields("triggered"),
            "outcome_record": {"outcome": "success", "deepest_stage": "Trigger"},
        },
        {
            "sample_id": "fp",
            "project": "p2",
            **_behavioral_fields("false positive crash"),
            "false_positive": True,
            "false_positive_pocs": 1,
            "outcome_record": {"outcome": "partial", "deepest_stage": "Sink"},
        },
        {
            "sample_id": "fail",
            "project": "p3",
            **_behavioral_fields("parser rejected every candidate"),
            "outcome_record": {"outcome": "failure", "first_failed_stage": "Parser"},
        },
    ])

    panel = build_validation_panel(pools, through_batch=0, success_count=1, crash_count=1, failure_count=1)

    assert panel["protocol"] == "skill-update-validation-panel-v1"
    assert [item["role"] for item in panel["samples"]] == [
        "success_preservation",
        "crash_reference",
        "failure_recovery",
    ]
    assert [item["sample_id"] for item in panel["samples"]] == ["ok", "fp", "fail"]


def test_validation_panel_rotates_by_panel_index():
    pools = empty_pools()
    records = []
    for i in range(4):
        records.append({
            "sample_id": f"ok{i}",
            "project": "p",
            **_behavioral_fields("triggered"),
            "outcome_record": {"outcome": "success", "deepest_stage": "Trigger"},
        })
        records.append({
            "sample_id": f"fp{i}",
            "project": "p",
            **_behavioral_fields("false positive crash"),
            "false_positive": True,
            "false_positive_pocs": 1,
            "outcome_record": {"outcome": "partial", "deepest_stage": "Sink"},
        })
        records.append({
            "sample_id": f"fail{i}",
            "project": "p",
            **_behavioral_fields("parser rejected every candidate"),
            "outcome_record": {"outcome": "failure", "first_failed_stage": "Parser"},
        })
    update_pools(pools, 0, records)

    p0 = build_validation_panel(pools, through_batch=0, success_count=2, crash_count=2, failure_count=2, panel_index=0)
    p1 = build_validation_panel(pools, through_batch=0, success_count=2, crash_count=2, failure_count=2, panel_index=1)

    assert len(p0["samples"]) == 6
    assert len(p1["samples"]) == 6
    assert [item["sample_id"] for item in p0["samples"]] != [item["sample_id"] for item in p1["samples"]]
    assert p1["panel_index"] == 1
