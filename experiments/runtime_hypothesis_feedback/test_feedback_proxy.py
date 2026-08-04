import json
from pathlib import Path
from tempfile import TemporaryDirectory

from experiments.runtime_hypothesis_feedback.feedback_proxy import (
    _apply_ordered_stage_gate,
    _is_observational_expression,
    _normalized_stage_state,
    _same_source_file,
    _trace_checkpoints,
    accumulate_poc_reward,
    compact_online_feedback,
    candidate_delta,
    summarize_feedback,
)


def test_ordered_gate_blocks_propagation_when_root_is_unresolved():
    gated = _apply_ordered_stage_gate({
        "admission": "confirmed",
        "root": "unresolved",
        "propagation": "confirmed",
        "target": "not_reached",
    })
    assert gated == {
        "admission": "confirmed",
        "root": "unresolved",
        "propagation": "observed_but_blocked",
        "target": "not_reached",
    }


def test_authoritative_target_confirms_ordered_prerequisites():
    gated = _apply_ordered_stage_gate({
        "admission": "not_reached",
        "root": "unresolved",
        "propagation": "not_reached",
        "target": "confirmed",
    })
    assert gated == {
        "admission": "confirmed",
        "root": "confirmed",
        "propagation": "confirmed",
        "target": "confirmed",
    }


def test_distinct_candidate_delta_uses_runtime_evidence_not_trace_text():
    previous = {
        "reward": {
            "admission": "confirmed", "root": "not_reached",
            "propagation": "blocked_on_root", "target": "not_triggered",
        },
        "steps": [{
            "exact_hit": True, "observed_file": "p.c",
            "observed_function": "parse", "observed_line": 4,
        }],
    }
    current = {
        "reward": {
            "admission": "confirmed", "root": "location_reached_only",
            "propagation": "consumer_not_declared", "target": "not_triggered",
        },
        "steps": [
            {
                "exact_hit": True, "observed_file": "p.c",
                "observed_function": "parse", "observed_line": 4,
            },
            {
                "exact_hit": True, "observed_file": "p.c",
                "observed_function": "copy", "observed_line": 9,
            },
        ],
    }
    with TemporaryDirectory() as directory:
        task_dir = Path(directory)
        attempt = task_dir / "old"
        attempt.mkdir()
        (attempt / "feedback.json").write_text(json.dumps({
            "poc_sha256": "old-hash",
            "hypothesis_feedback": previous,
        }), encoding="utf-8")
        delta = candidate_delta(task_dir, "new-hash", current, None)
    assert delta["compared_to_previous_distinct_candidate"] is True
    assert delta["new_runtime_locations"] == ["p.c:copy:9"]
    assert delta["stage_state"]["root"] == "unresolved"
    assert delta["root_evidence_changed"] is True


def test_agent_trace_becomes_exact_and_function_checkpoints():
    trace = [{
        "step": 1,
        "file": "src/parser.c",
        "function": "parse",
        "line": 42,
        "var": "length",
        "code": "read(length)",
        "note": "input",
        "role": "source",
        "captures": {"length": "(long)length"},
    }]
    checkpoints = _trace_checkpoints(trace)
    assert [item["event_point"] for item in checkpoints] == [
        "exact:1",
        "function:1",
    ]
    assert checkpoints[0]["captures"] == {"length": "(long)length"}
    assert "captures" not in checkpoints[1]


def test_observer_capture_is_retried_at_function_entry():
    trace = [{
        "step": 1,
        "file": "src/parser.c",
        "function": "parse",
        "line": 42,
        "captures": {"observer_length": "length", "agent_value": "value"},
        "observer_capture_names": ["observer_length"],
    }]
    checkpoints = _trace_checkpoints(trace)
    assert checkpoints[0]["captures"] == {
        "observer_length": "length", "agent_value": "value"
    }
    assert checkpoints[0]["max_hits_per_breakpoint"] == 256
    assert checkpoints[1]["captures"] == {"observer_length": "length"}
    assert "max_hits_per_breakpoint" not in checkpoints[1]


def test_function_entry_capture_recovers_optimized_exact_line_value():
    trace = [{
        "step": 1,
        "file": "src/parser.c",
        "function": "parse",
        "captures": {"observer_length": "length"},
        "observer_capture_names": ["observer_length"],
    }]
    feedback = summarize_feedback(
        trace,
        [
            {
                "event_point": "function:1", "file": "src/parser.c",
                "fields": {"observer_length": 80}, "event_sequence": 1,
            },
            {
                "event_point": "exact:1",
                "capture_errors": {"observer_length": "optimized out"},
                "event_sequence": 2,
            },
        ],
        duplicate=False,
    )
    assert feedback["steps"][0]["fields"] == {"observer_length": 80}
    assert feedback["steps"][0]["capture_errors"] == {}
    assert feedback["steps"][0]["capture_sources"] == {
        "observer_length": "function_entry"
    }


def test_only_compatible_addresses_receive_deterministic_delta_relation():
    trace = [
        {
            "step": 1, "captures": {"observer_end": "lexer->end"},
            "observer_capture_names": ["observer_end"],
            "observer_capture_kinds": {"observer_end": "address"},
        },
        {
            "step": 2, "captures": {
                "observer_byte": "*lexer->pos", "observer_pos": "lexer->pos"
            },
            "observer_capture_names": ["observer_byte", "observer_pos"],
            "observer_capture_kinds": {
                "observer_byte": "dereferenced_value", "observer_pos": "address"
            },
        },
    ]
    feedback = summarize_feedback(
        trace,
        [
            {"event_point": "exact:1", "fields": {"observer_end": 100}},
            {"event_point": "exact:2", "fields": {
                "observer_byte": 65, "observer_pos": 99
            }},
        ],
        duplicate=False,
    )
    assert feedback["runtime_relations"] == [{
        "kind": "address_delta",
        "left": {
            "step": 1, "name": "observer_end",
            "expression": "lexer->end", "value": 100,
        },
        "operator": "subtract",
        "right": {
            "step": 2, "name": "observer_pos",
            "expression": "lexer->pos", "value": 99,
        },
        "result": 1,
    }]


def test_unrelated_or_ambiguous_capture_values_are_never_compared():
    trace = [
        {
            "step": 1, "captures": {"observer_end": "end"},
            "observer_capture_names": ["observer_end"],
            "observer_capture_kinds": {"observer_end": "address"},
        },
        {
            "step": 2, "captures": {"observer_pos": "lexer->pos"},
            "observer_capture_names": ["observer_pos"],
            "observer_capture_kinds": {"observer_pos": "address"},
        },
        {
            "step": 3, "captures": {"observer_size": "size"},
            "observer_capture_names": ["observer_size"],
            "observer_capture_kinds": {"observer_size": "scalar_or_pointer"},
        },
    ]
    feedback = summarize_feedback(
        trace,
        [
            {"event_point": "exact:1", "fields": {"observer_end": 100}},
            {"event_point": "exact:2", "fields": {"observer_pos": 99}},
            {"event_point": "exact:3", "fields": {"observer_size": 101}},
        ],
        duplicate=False,
    )
    assert feedback["runtime_relations"] == []


def test_literal_capture_is_not_forwarded_to_gdb():
    trace = [{
        "step": 1,
        "captures": {"claimed": "(long)3", "actual": "(long)length"},
    }]
    checkpoints = _trace_checkpoints(trace)
    # Without a source line or function there is no executable checkpoint.
    assert checkpoints == []
    assert _is_observational_expression("(long)3") is False
    assert _is_observational_expression("(long)length") is True
    assert _is_observational_expression("state->length") is True


def test_function_fallback_rejects_same_named_function_in_wrong_file():
    trace = [{
        "step": 1,
        "file": "src/PJ_igh.c",
        "function": "s_forward",
        "role": "root",
        "invariant": "index must remain in bounds",
    }]
    hits = [{
        "event_point": "function:1",
        "file": "./src/PJ_moll.c",
        "function": "s_forward",
        "line": 16,
    }]
    feedback = summarize_feedback(trace, hits, duplicate=False, target_exit_code=0)
    assert feedback["root"]["reached"] is False
    assert feedback["steps"][0]["function_hit"] is False
    assert feedback["steps"][0]["function_file_mismatch"] == {
        "declared_file": "src/PJ_igh.c",
        "observed_file": "./src/PJ_moll.c",
    }
    assert _same_source_file("src/PJ_igh.c", "/work/proj/src/PJ_igh.c")
    assert _same_source_file(
        "repo-vul/src-vul/lwan/src/lib/lwan-config.c",
        "/src/lwan/src/lib/lwan-config.c",
    )
    assert _same_source_file("src-vul/kimgio_fuzzer.cc", "/src/kimgio_fuzzer.cc")
    assert not _same_source_file(
        "repo-vul/src-vul/a/src/parser.c",
        "/src/b/lib/parser.c",
    )


def test_invalid_root_anchor_does_not_contradict_issue_stage():
    trace = [{
        "step": 1,
        "file": "src/decoder.cpp",
        "function": "Load",
        "line": 10,
        "code": "readRawData(dst, count);",
        "role": "root",
        "anchor_validation": {
            "status": "invalid",
            "reason": "line_code_mismatch",
            "source_text": "static bool Load(...) {",
            "matching_lines": [42],
        },
    }]
    feedback = summarize_feedback(
        trace,
        [{
            "event_point": "function:1",
            "file": "/src/decoder.cpp",
            "function": "Load",
            "line": 10,
        }],
        duplicate=False,
        target_exit_code=0,
    )
    assert feedback["exactly_observed_steps"] == 0
    assert feedback["root"]["status"] == "location_reached_only"
    assert feedback["diagnosis"] == "issue_root_condition_not_confirmed"
    assert feedback["candidate_verdict"] == {
        "source_anchor": "contradicted",
        "contradicted_anchor_steps": [1],
        "repaired_anchor_steps": [],
        "runtime_hypothesis": "unresolved",
        "scope": "candidate_claims_only_not_alternative_vulnerability",
    }
    assert _normalized_stage_state(feedback, None)["root"] == "unresolved"
    compact = compact_online_feedback(
        feedback,
        accumulated_reward=feedback["reward"],
        evidence_changed=True,
        normalized_stage_state=_normalized_stage_state(feedback, None),
    )
    assert compact["root"]["status"] == "unresolved"
    assert "declared source anchor is contradicted" in compact["summary"]
    assert "stage remains unresolved rather than contradicted" in compact["summary"]
    assert compact["step_evidence"][0]["anchor_validation"]["status"] == "invalid"


def test_relocated_exact_breakpoint_is_rejected():
    trace = [{
        "step": 1, "file": "src/decoder.cpp", "function": "Load", "line": 42,
        "role": "root",
        "anchor_validation": {"status": "valid", "reason": "line_code_match"},
    }]
    feedback = summarize_feedback(trace, [{
        "event_point": "exact:1",
        "file": "/src/decoder.cpp",
        "function": "Load",
        "line": 10,
    }], duplicate=False)
    assert feedback["steps"][0]["exact_hit"] is False
    assert feedback["steps"][0]["exact_anchor_error"]["reason"] == (
        "exact_breakpoint_line_relocated"
    )


def test_read_raw_data_call_and_return_are_joined_into_runtime_fact():
    feedback = summarize_feedback([], [
        {
            "kind": "call_observation",
            "event_point": "runtime_call:readRawData:1",
            "call_instance_id": "call:1",
            "call_name": "readRawData",
            "file": "/src/decoder.cpp",
            "function": "Load",
            "line": 42,
            "source_code": "stream.readRawData(dst, count);",
            "requested_capture": "requested_bytes",
            "return_capture": "returned_bytes",
            "branch_captures": ["packet_is_rle", "palette_mode", "grey_mode"],
            "static_branch_facts": {"packet_is_rle": False},
            "fields": {
                "requested_bytes": 8,
                "packet_is_rle": True,
                "palette_mode": True,
                "grey_mode": False,
            },
            "event_sequence": 3,
        },
        {
            "kind": "call_return_observation",
            "event_point": "runtime_call:readRawData:1:return",
            "call_instance_id": "call:1",
            "fields": {"returned_bytes": 3},
            "event_sequence": 4,
        },
    ], duplicate=False)
    assert feedback["runtime_call_observations"] == [{
        "call_instance_id": "call:1",
        "call_name": "readRawData",
        "assertion_role": [],
        "actual_callsite": {
            "file": "/src/decoder.cpp", "function": "Load", "line": 42,
            "source_code": "stream.readRawData(dst, count);",
        },
        "requested_bytes": 8,
        "returned_bytes": 3,
        "short_read": True,
        "source_requested_expression": None,
        "branch_facts": {
            "packet_is_rle": False,
            "palette_mode": True,
            "grey_mode": False,
        },
        "arguments": [],
        "return_value": {"name": "returned_bytes", "value": 3},
        "derived_relations": [],
        "capture_errors": {},
        "call_sequence": 3,
        "return_sequence": 4,
    }]


def test_executed_source_branch_facts_are_recorded():
    feedback = summarize_feedback([], [{
        "kind": "branch_observation",
        "event_point": "runtime_branch:input_mode:3",
        "file": "/src/decoder.cpp",
        "function": "Load",
        "line": 77,
        "static_branch_facts": {"palette_mode": False, "grey_mode": False},
        "event_sequence": 9,
    }], duplicate=False)
    assert feedback["runtime_branch_observations"] == [{
        "event_point": "runtime_branch:input_mode:3",
        "actual_location": {
            "file": "/src/decoder.cpp", "function": "Load", "line": 77,
        },
        "branch_facts": {"palette_mode": False, "grey_mode": False},
        "event_sequence": 9,
    }]


def test_feedback_checks_declared_condition_without_gt():
    trace = [
        {
            "step": 1,
            "role": "root",
            "condition": {"op": "gt", "left": "length", "right": 63},
        },
        {"step": 2, "role": "sink"},
    ]
    feedback = summarize_feedback(
        trace,
        [{
            "event_point": "exact:1",
            "file": "p.c",
            "function": "parse",
            "line": 10,
            "fields": {"length": 80},
        }],
        duplicate=False,
    )
    assert feedback["uses_hidden_gt"] is False
    assert feedback["steps"][0]["condition_satisfied"] is True
    assert feedback["first_unobserved_step"] == 2


def test_feedback_separates_trace_input_path_state_and_target():
    trace = [
        {"step": 1, "phase": "admission", "role": "source"},
        {
            "step": 2,
            "role": "root",
            "condition": {"op": "gt", "left": "length", "right": 63},
        },
    ]
    hits = [
        {"event_point": "exact:1", "file": "p.c", "function": "parse", "line": 4},
        {
            "event_point": "exact:2",
            "file": "p.c",
            "function": "copy",
            "line": 9,
            "fields": {"length": 12},
        },
    ]
    feedback = summarize_feedback(
        trace,
        hits,
        duplicate=False,
        target_exit_code=0,
    )
    assert feedback["trace_format"] == {"valid": True, "error": None}
    assert feedback["admission"]["status"] == "confirmed"
    assert feedback["path"]["status"] == "fully_observed"
    assert feedback["state"]["failed"] == 1
    assert feedback["root"]["status"] == "candidate_condition_false"
    assert feedback["downstream_propagation"]["status"] == "consumer_not_declared"
    assert feedback["target"]["triggered"] is False
    assert feedback["diagnosis"] == "candidate_root_condition_false"
    assert feedback["reward"] == {
        "admission": "confirmed",
        "root": "candidate_condition_false",
        "propagation": "consumer_not_declared",
        "target": "not_triggered",
    }
    assert feedback["diagnostics"] == {"propagation": "consumer_not_declared"}


def test_literal_only_state_claim_is_unresolved_not_rewarded():
    trace = [{
        "step": 1,
        "role": "root",
        "captures": {"length": "(long)3", "capacity": "(long)6"},
        "condition": {"op": "lt", "left": "length", "right": "capacity"},
    }]
    feedback = summarize_feedback(
        trace,
        [{"event_point": "exact:1", "fields": {}}],
        duplicate=False,
        target_exit_code=0,
    )
    assert feedback["state"]["status"] == "unresolved"
    assert feedback["state"]["satisfied"] == 0
    assert feedback["steps"][0]["capture_rejections"] == {
        "length": "literal-only expression is not a runtime observation",
        "capacity": "literal-only expression is not a runtime observation",
    }
    assert feedback["root"]["condition_trusted"] is False
    assert feedback["root"]["status"] == "candidate_condition_unresolved"
    assert feedback["diagnosis"] == "candidate_root_condition_unresolved"


def test_propagation_order_is_a_non_reward_diagnostic():
    trace = [
        {
            "step": 1,
            "role": "root",
            "function": "decode",
            "invariant": "decoded output must be valid",
        },
        {"step": 2, "role": "sink", "function": "consume"},
    ]
    hits = [
        {"event_point": "exact:1", "event_sequence": 2},
        {"event_point": "exact:2", "event_sequence": 1},
    ]
    feedback = summarize_feedback(trace, hits, duplicate=False, target_exit_code=0)
    assert feedback["path"]["status"] == "fully_observed"
    assert feedback["path"]["first_out_of_order_step"] is None
    assert feedback["path"]["order_scope"] == "explicit_root_to_consumer_only"
    assert feedback["diagnostics"]["propagation"] == "consumer_out_of_order"
    assert feedback["diagnosis"] == "issue_root_condition_not_confirmed"


def test_nested_exact_callbacks_do_not_create_false_global_order_failure():
    trace = [
        {"step": 1, "function": "wrapper"},
        {"step": 2, "function": "callee"},
    ]
    # The wrapper's declared exact line executes after the nested callee, while
    # the function-entry callbacks preserve the causal call order.
    hits = [
        {"event_point": "function:1", "event_sequence": 1},
        {"event_point": "function:2", "event_sequence": 2},
        {"event_point": "exact:2", "event_sequence": 3},
        {"event_point": "exact:1", "event_sequence": 4},
    ]
    feedback = summarize_feedback(trace, hits, duplicate=False, target_exit_code=0)
    assert feedback["path"]["status"] == "fully_observed"
    assert feedback["path"]["first_out_of_order_step"] is None
    assert [step["observed_sequence"] for step in feedback["steps"]] == [1, 2]


def test_root_return_in_same_function_is_not_downstream_propagation():
    trace = [
        {
            "step": 1,
            "role": "root",
            "function": "decrypt",
            "invariant": "output must be valid UTF-8",
        },
        {"step": 2, "role": "sink", "function": "decrypt"},
    ]
    hits = [
        {"event_point": "exact:1", "timestamp": 1.0},
        {"event_point": "exact:2", "timestamp": 2.0},
    ]
    feedback = summarize_feedback(trace, hits, duplicate=False, target_exit_code=0)
    assert feedback["root"]["status"] == "location_reached_only"
    assert feedback["downstream_propagation"]["status"] == "consumer_not_declared"
    assert feedback["reward"] == {
        "admission": "unavailable",
        "root": "location_reached_only",
        "propagation": "consumer_not_declared",
        "target": "not_triggered",
    }
    assert feedback["diagnostics"] == {"propagation": "consumer_not_declared"}
    assert feedback["diagnosis"] == "issue_root_condition_not_confirmed"


def test_distinct_downstream_consumer_is_rewarded_after_root():
    trace = [
        {
            "step": 1,
            "role": "root",
            "function": "decrypt",
            "invariant": "output must be valid UTF-8",
            "condition": {"op": "eq", "left": "invalid", "right": 1},
        },
        {"step": 2, "role": "sink", "function": "format_number"},
    ]
    hits = [
        {
            "event_point": "exact:1",
            "timestamp": 1.0,
            "fields": {"invalid": 1},
        },
        {"event_point": "exact:2", "timestamp": 2.0},
    ]
    feedback = summarize_feedback(
        trace,
        hits,
        duplicate=False,
        target_exit_code=0,
        trusted_root_condition=True,
    )
    assert feedback["downstream_propagation"]["status"] == "consumer_reached_after_root"
    assert feedback["reward"]["propagation"] == "consumer_reached_after_root"
    compact = compact_online_feedback(
        feedback,
        accumulated_reward={**feedback["reward"], "admission": "confirmed"},
        evidence_changed=True,
    )
    assert compact["next_gap"] == "target"
    assert compact["step_evidence"][0]["captured_values"] == {"invalid": 1}
    assert feedback["diagnosis"] == "candidate_root_condition_satisfied_without_target"


def test_observed_root_location_does_not_advance_to_propagation():
    detail = {
        "duplicate_poc": False,
        "trace_format": {"valid": True, "error": None},
    }
    compact = compact_online_feedback(
        detail,
        accumulated_reward={
            "admission": "location_reached_only",
            "root": "location_reached_only",
            "propagation": "consumer_not_declared",
            "target": "not_triggered",
        },
        evidence_changed=True,
    )
    assert compact["next_gap"] == "root"
    assert "vulnerable state" in compact["summary"]
    assert "optional_root_observation_protocol" not in compact
    assert compact["root"]["status"] == "unresolved"


def test_candidate_root_condition_false_is_dense_non_gt_feedback():
    compact = compact_online_feedback(
        {"trace_format": {"valid": True, "error": None}, "steps": []},
        accumulated_reward={
            "admission": "confirmed",
            "root": "candidate_condition_false",
            "propagation": "consumer_reached_after_root",
            "target": "not_triggered",
        },
        evidence_changed=True,
    )
    assert compact["next_gap"] == "root"
    assert "evaluated false" in compact["summary"]
    assert compact["propagation"] == {
        "status": "consumer_reached_after_root",
        "blocking": False,
    }


def test_candidate_condition_true_does_not_confirm_issue_root():
    compact = compact_online_feedback(
        {"trace_format": {"valid": True, "error": None}, "steps": []},
        accumulated_reward={
            "admission": "confirmed",
            "root": "candidate_condition_satisfied",
            "propagation": "consumer_not_declared",
            "target": "not_triggered",
        },
        evidence_changed=True,
    )
    assert compact["next_gap"] == "root"
    assert compact["root"] == {
        "status": "unresolved",
        "candidate_checkpoint_status": "candidate_condition_satisfied",
    }
    assert compact["propagation"]["blocking"] is False


def test_v6_keeps_candidate_condition_sparse_for_matched_ab_control():
    trace = [{
        "step": 1,
        "role": "root",
        "condition": {"op": "gt", "left": "offset", "right": 8},
    }]
    hits = [{"event_point": "exact:1", "fields": {"offset": 0}}]
    feedback = summarize_feedback(
        trace, hits, duplicate=False, reward_protocol="v6"
    )
    assert feedback["state"]["status"] == "unsatisfied"
    assert feedback["root"]["status"] == "location_reached_only"
    compact = compact_online_feedback(
        feedback,
        accumulated_reward=feedback["reward"],
        evidence_changed=True,
        reward_protocol="v6",
    )
    assert compact["reward_protocol"] == "v6"
    assert compact["next_gap"] == "root"
    assert "optional_root_observation_protocol" not in compact
    assert compact["propagation"]["blocking"] is True


def test_v6_online_evidence_cannot_be_misread_as_program_hit_counts():
    trace = [
        {"step": 1, "file": "p.c", "function": "scan", "line": 10},
        {"step": 2, "file": "p.c", "function": "scan", "line": 10},
    ]
    feedback = summarize_feedback(
        trace,
        [
                {"event_point": "exact:1", "file": "p.c", "line": 10, "hit_count": 1},
            {"event_point": "exact:2", "file": "p.c", "line": 10, "hit_count": 1},
        ],
        duplicate=False,
        reward_protocol="v6",
    )
    assert feedback["steps"][0]["checkpoint_sample_ordinal"] == 1
    assert "observed_hit_count" not in feedback["steps"][0]

    compact = compact_online_feedback(
        feedback,
        accumulated_reward=feedback["reward"],
        evidence_changed=True,
        reward_protocol="v6",
    )
    assert all(
        "observed_hit_count" not in step
        and "checkpoint_sample_ordinal" not in step
        for step in compact["step_evidence"]
    )
    assert compact["evidence_semantics"] == {
        "scope": "bounded_checkpoint_observations_per_declared_trace_step",
        "program_execution_counts_available": False,
        "observed_sequence_is_complete_control_flow": False,
        "same_location_steps_are_independent_checkpoints": True,
    }


def test_invalid_trace_is_diagnosed_before_runtime_feedback():
    feedback = summarize_feedback(
        [],
        [],
        duplicate=False,
        trace_valid=False,
        trace_error="missing field: function",
        runtime_checked=False,
    )
    assert feedback["diagnosis"] == "trace_format_invalid"
    assert feedback["trace_format"]["error"] == "missing field: function"


def test_function_fallback_is_location_only_for_admission_and_root():
    trace = [{
        "step": 1,
        "file": "parser.c",
        "function": "parse",
        "phase": "admission",
        "role": "root",
        "invariant": "length must fit",
    }]
    feedback = summarize_feedback(
        trace,
        [{
            "event_point": "function:1",
            "file": "/src/parser.c",
            "function": "parse",
        }],
        duplicate=False,
        target_exit_code=0,
    )
    assert feedback["admission"]["status"] == "location_reached_only"
    assert feedback["root"]["status"] == "location_reached_only"


def test_only_verifier_owned_condition_can_confirm_root():
    trace = [{
        "step": 1,
        "role": "root",
        "condition": {"op": "gt", "left": "length", "right": 8},
    }]
    hits = [{"event_point": "exact:1", "fields": {"length": 12}}]
    untrusted = summarize_feedback(trace, hits, duplicate=False)
    trusted = summarize_feedback(
        trace, hits, duplicate=False, trusted_root_condition=True
    )
    assert untrusted["root"]["status"] == "candidate_condition_satisfied"
    assert trusted["root"]["status"] == "condition_confirmed"


def test_source_compiled_call_relation_can_confirm_root():
    trace = [{
        "step": 1,
        "role": "root",
        "file": "decoder.cpp",
        "function": "decode",
        "line": 42,
        "anchor_validation": {"status": "valid"},
    }]
    hits = [
        {
            "event_point": "exact:1", "file": "/src/decoder.cpp",
            "function": "decode", "line": 42,
        },
        {
            "kind": "call_observation", "event_point": "runtime_call:1",
            "call_instance_id": "call:1", "assertion_role": ["root"],
            "return_capture": "return_value",
            "argument_metadata": [{
                "index": 1, "name": "length", "source_expression": "length",
            }],
            "derived_relations": [{
                "name": "return_value_lt_length", "op": "lt",
                "left": "return_value", "right": "length",
            }],
            "fields": {"length": 8},
        },
        {
            "kind": "call_return_observation",
            "call_instance_id": "call:1", "fields": {"return_value": 3},
        },
    ]
    feedback = summarize_feedback(trace, hits, duplicate=False)
    assert feedback["root"]["runtime_relation_confirmed"] is True
    assert feedback["root"]["status"] == "condition_confirmed"


def test_trace_error_exit_two_is_not_target_success():
    feedback = summarize_feedback(
        [], [], duplicate=False, trace_valid=False, target_exit_code=2,
        runtime_checked=False,
    )
    assert feedback["target"]["triggered"] is False


def test_duplicate_poc_reward_is_monotonic_and_compact():
    with TemporaryDirectory() as directory:
        task_dir = Path(directory)
        old_dir = task_dir / "old"
        old_dir.mkdir()
        (old_dir / "feedback.json").write_text(json.dumps({
            "poc_sha256": "same",
            "accumulated_reward": {
                "admission": "confirmed",
                "root": "location_reached_only",
                "target": "not_triggered",
            },
        }))
        current = {
            "admission": "not_reached",
            "root": "not_reached",
            "propagation": "blocked_on_root",
            "target": "not_triggered",
        }
        accumulated, changed = accumulate_poc_reward(task_dir, "same", current)
        assert accumulated == {
            "admission": "confirmed",
            "root": "location_reached_only",
            "propagation": "blocked_on_root",
            "target": "not_triggered",
        }
        assert changed is False
        detail = summarize_feedback([], [], duplicate=True, target_exit_code=0)
        compact = compact_online_feedback(
            detail,
            accumulated_reward=accumulated,
            evidence_changed=changed,
            skeleton={"root_hypothesis": {"predicate": "object is stale"}},
        )
        assert compact["next_gap"] == "root"
        assert compact["new_runtime_evidence"] is False
        assert "duplicate" in compact["summary"]
        assert "Change the candidate" not in compact["summary"]


def test_duplicate_trace_rewrite_cannot_turn_sampled_false_into_progress():
    with TemporaryDirectory() as directory:
        task_dir = Path(directory)
        old_dir = task_dir / "old"
        old_dir.mkdir()
        (old_dir / "feedback.json").write_text(json.dumps({
            "poc_sha256": "same",
            "accumulated_reward": {
                "admission": "confirmed",
                "root": "location_reached_only",
                "propagation": "consumer_not_declared",
                "target": "not_triggered",
            },
        }))
        accumulated, changed = accumulate_poc_reward(task_dir, "same", {
            "admission": "confirmed",
            "root": "candidate_condition_false",
            "propagation": "consumer_not_declared",
            "target": "not_triggered",
        })
    assert accumulated["root"] == "location_reached_only"
    assert changed is False
