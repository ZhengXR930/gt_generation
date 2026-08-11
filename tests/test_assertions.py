import json
import sys
from pathlib import Path

import pytest
from gt_generation.gt_toolkit import assertions as assertions_module

from gt_generation.gt_toolkit.assertions import (
    annotate_scored_invariants,
    assertion_content_hash,
    build_assertion_reward_spec,
    build_perturbation_results,
    build_verified_invariants,
    build_verified_assertions,
    check_msan_offset,
    evaluate_assertion,
    freeze_spec,
    parse_msan_uninit,
    parse_trace_matrix,
    validate_assertions,
    validate_binding_coverage,
    validate_frozen_spec,
    validate_invariant_bindings,
)


FIXTURE = Path(__file__).parent / "fixtures" / "assertion_pilot_12595"


def _inputs():
    spec = json.loads((FIXTURE / "candidate_assertions.json").read_text())
    vulnerable = parse_trace_matrix((FIXTURE / "vulnerable_trace.txt").read_text())
    fixed = parse_trace_matrix((FIXTURE / "fixed_trace.txt").read_text())
    return spec, vulnerable, fixed


def _contract_node(invariant_id, role, operands=None, relation=None):
    operand_list = operands or [invariant_id]
    if relation is None:
        relation = {"op": "same_object", "left": operand_list[0], "right": operand_list[0]}
    return {
        "invariant_id": invariant_id,
        "role": role,
        "file": "src/parser.c",
        "function": "parse",
        "line": 10,
        "operands": operand_list,
        "relation": relation,
        "verified": True,
    }


def _contract_edge(invariant_id, from_node, to_node, operands=None, relation=None):
    operand_list = operands or ["value"]
    if relation is None:
        relation = {"op": "eq", "left": operand_list[0], "right": operand_list[0]}
    return {
        "invariant_id": invariant_id,
        "type": "data",
        "from_node": from_node,
        "to_node": to_node,
        "operands": operand_list,
        "relation": relation,
        "verified": True,
    }


def _root_node(invariant_id="root_cause_criterion"):
    return _contract_node(
        invariant_id,
        "root_cause",
        ["raw_count", "-3"],
        {"op": "ge", "left": "raw_count", "right": "-3"},
    )


def _freeze_dict(spec):
    spec["content_hash"] = assertion_content_hash(spec)
    return spec


def _minimal_v3_spec(assertions):
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": assertions,
    }
    return _freeze_dict(spec)


def test_required_assertion_distinguishes_violation_guard_and_genuine_execution():
    spec, vulnerable, fixed = _inputs()
    assertion = next(item for item in spec["assertions"] if item["kind"] == "required")

    assert evaluate_assertion(assertion, vulnerable["int_min"], "vulnerable")["status"] == "violated"
    assert evaluate_assertion(assertion, fixed["int_min"], "fixed")["status"] == "guarded"
    assert evaluate_assertion(assertion, fixed["neg3"], "fixed")["status"] == "genuine"


def test_transition_pairs_first_target_after_latest_relevant_source():
    case = parse_trace_matrix(
        "CASE name=original rc=0 result=clean\n"
        "ASSERT_EVT point=target value=1\n"
        "ASSERT_EVT point=source value=24\n"
        "ASSERT_EVT point=target value=24\n"
        "ASSERT_EVT point=target value=4\n"
        "ENDCASE\n"
    )["original"]
    assertion = {
        "id": "edge.request",
        "kind": "transition",
        "from": "source",
        "at": "target",
        "check": ["eq", "$source.value", "$target.value"],
    }

    result = evaluate_assertion(assertion, case, "vulnerable")

    assert result["satisfied"] is True
    assert result["from_index"] == 1
    assert result["to_index"] == 2


def test_minimal_assertions_validate_on_vulnerable_fixed_differential():
    spec, vulnerable, fixed = _inputs()
    results = validate_assertions(spec, vulnerable, fixed)

    assert results["all_verified"] is True
    assert len(results["assertions"]) == 4
    required = next(item for item in results["assertions"] if item["kind"] == "required")
    assert required["genuine_witness_case"] == "neg3"
    perturbations = build_perturbation_results(spec, results)
    assert perturbations["needed"] is True
    assert perturbations["genuine_witness_cases"] == {
        "required.raw_count_lower_bound": "neg3"
    }
    neg3 = next(item for item in perturbations["cases"] if item["name"] == "neg3")
    assert neg3["assertions"][0] == {
        "id": "required.raw_count_lower_bound",
        "vulnerable_status": "genuine",
        "fixed_status": "genuine",
        "fixed_protected_event": True,
        "useful": True,
    }


def test_guarded_required_assertion_fails_without_genuine_perturbation():
    spec, vulnerable, fixed = _inputs()
    original = spec["original_case"]
    results = validate_assertions(
        spec,
        {original: vulnerable[original]},
        {original: fixed[original]},
    )

    required = next(item for item in results["assertions"] if item["kind"] == "required")
    assert required["verified"] is False
    assert required["genuine_witness_case"] is None
    assert "protected event" in required["verification_error"]
    assert results["all_verified"] is False


def test_assertion_cli_exits_nonzero_when_perturbation_gate_fails(tmp_path, monkeypatch):
    spec, vulnerable, fixed = _inputs()
    original = spec["original_case"]
    vulnerable_trace = tmp_path / "vulnerable.txt"
    fixed_trace = tmp_path / "fixed.txt"
    source_vulnerable = (FIXTURE / "vulnerable_trace.txt").read_text()
    source_fixed = (FIXTURE / "fixed_trace.txt").read_text()
    vulnerable_trace.write_text(source_vulnerable.split("CASE name=neg4", 1)[0])
    fixed_trace.write_text(source_fixed.split("CASE name=neg4", 1)[0])
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    results_path = tmp_path / "results.json"
    perturbations_path = tmp_path / "perturbations.json"
    monkeypatch.setattr(sys, "argv", [
        "assertions",
        "--spec", str(spec_path),
        "--vulnerable-trace", str(vulnerable_trace),
        "--fixed-trace", str(fixed_trace),
        "--results-out", str(results_path),
        "--perturbation-results-out", str(perturbations_path),
    ])

    with pytest.raises(SystemExit) as exc:
        assertions_module.main()
    assert exc.value.code == 1
    assert json.loads(results_path.read_text())["all_verified"] is False
    perturbations = json.loads(perturbations_path.read_text())
    assert perturbations["needed"] is True
    assert perturbations["all_needed_witnessed"] is False


def test_avoided_required_assertion_requests_genuine_perturbation():
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "avoided",
        "original_case": "original",
        "assertions": [
            {
                "id": "required.guard",
                "invariants": ["root"],
                "kind": "required",
                "at": "guard",
                "protects": "sink",
                "check": ["eq", "$guard.is_separator", "$guard.branch_taken"],
            }
        ],
    }
    spec = _freeze_dict(spec)
    vulnerable = parse_trace_matrix(
        "CASE name=original rc=1 result=crash\n"
        "ASSERT_EVT point=guard is_separator=1 branch_taken=0\n"
        "ASSERT_EVT point=sink value=1\n"
        "ENDCASE\n"
    )
    fixed = parse_trace_matrix(
        "CASE name=original rc=0 result=clean\n"
        "ASSERT_EVT point=guard is_separator=1 branch_taken=1\n"
        "ENDCASE\n"
    )

    results = validate_assertions(spec, vulnerable, fixed)

    required = results["assertions"][0]
    assert required["matrix"]["fixed"]["original"]["status"] == "avoided"
    assert required["verified"] is False
    assert required["genuine_witness_case"] is None
    assert "fixed original is avoided" in required["verification_error"]
    perturbations = build_perturbation_results(spec, results)
    assert perturbations["needed"] is True
    assert perturbations["genuine_witness_cases"] == {"required.guard": None}


def test_frozen_hash_detects_post_execution_rewrite():
    spec, _, _ = _inputs()
    assert spec["content_hash"] == assertion_content_hash(spec)
    validate_frozen_spec(spec)

    spec["assertions"][0]["check"] = ["ne", "$stored_count", "$source_count"]
    with pytest.raises(ValueError, match="content hash mismatch"):
        validate_frozen_spec(spec)


def test_differential_status_confirmed_when_a_required_assertion_verifies():
    spec, vulnerable, fixed = _inputs()
    results = validate_assertions(spec, vulnerable, fixed)
    assert results["differential_status"] == "confirmed"
    required = next(item for item in results["assertions"] if item["kind"] == "required")
    assert required["verification"] == "differential"
    observed = next(item for item in results["assertions"] if item["kind"] == "observed")
    assert observed["verification"] == "vulnerable_side_only"


def test_differential_status_not_applicable_for_uninitialized_bugs():
    # An MSAN use-of-uninitialized-value bug cannot express its safety property as a
    # vulnerable/fixed field comparison, so the caller marks the differential N/A and
    # all_verified no longer reads as "differentially confirmed".
    spec, vulnerable, fixed = _inputs()
    results = validate_assertions(spec, vulnerable, fixed, differential_applicable=False)
    assert results["all_verified"] is True
    assert results["differential_status"] == "not_applicable"


def test_parse_msan_uninit_reads_offset_and_size():
    text = (
        "==7==WARNING: MemorySanitizer: use-of-uninitialized-value\n"
        "Uninitialized bytes in __interceptor_memcmp at offset 2 inside [0x7ffca5175600, 7)\n"
    )
    assert parse_msan_uninit(text) == {"init_prefix_len": 2, "access_size": 7}
    assert parse_msan_uninit("==1==ERROR: AddressSanitizer: heap-buffer-overflow") is None


def test_msan_offset_gate_passes_when_init_operand_matches_offset():
    # arvo_10131 shape: sink compares read_len 7 vs init_len 2; MSAN offset 2.
    results = {
        "original_case": "original",
        "assertions": [
            {"id": "A-SINK", "matrix": {"vulnerable": {"original": {"left": 7, "right": 2}}}},
        ],
    }
    check = check_msan_offset(results, {"init_prefix_len": 2, "access_size": 7})
    assert check["matched"] is True
    assert "error" not in check


def test_msan_offset_gate_flags_off_by_one_initialized_length():
    # arvo_10136 shape: sink compares cmp_bytes 21 vs bytes_written 5, but MSAN's
    # first uninitialized byte is at offset 4 -- the `len + 1` binding counted the
    # unwritten NUL terminator as initialized.
    results = {
        "original_case": "original",
        "assertions": [
            {"id": "A-SINK", "matrix": {"vulnerable": {"original": {"left": 21, "right": 5}}}},
        ],
    }
    check = check_msan_offset(results, {"init_prefix_len": 4, "access_size": 21})
    assert check["matched"] is False
    assert "off-by-one" in check["error"]


def test_msan_offset_gate_warns_when_read_size_not_witnessed():
    results = {
        "original_case": "original",
        "assertions": [
            {"id": "A", "matrix": {"vulnerable": {"original": {"left": 128, "right": 5}}}},
        ],
    }
    check = check_msan_offset(results, {"init_prefix_len": 4, "access_size": 21})
    assert check["matched"] is None
    assert "warning" in check and "error" not in check


def _uninit_invariants():
    """arvo_10136-shape: one reasoning edge, one eq identity-flow edge, one sink node."""
    verified_invariants = {
        "nodes": [
            {"invariant_id": "I-SINK", "verified_by": "A-SINK"},
        ],
        "edges": [
            {"invariant_id": "I-DECL-TO-READ", "verified_by": "A-DECL-TO-READ"},
            {"invariant_id": "I-READ-TO-DISPATCH", "verified_by": "A-READ-TO-DISPATCH"},
        ],
    }
    verified_assertions = {
        "assertions": [
            {"id": "A-SINK", "kind": "observed",
             "check": ["gt", "$oid_dispatch.cmp_bytes", "$oid_dispatch.oid_bytes_written"]},
            {"id": "A-DECL-TO-READ", "kind": "transition",
             "check": ["gt", "$oid_decl.sizeof_oid", "$oid_read.oid_bytes_written"]},
            {"id": "A-READ-TO-DISPATCH", "kind": "transition",
             "check": ["eq", "$oid_read.oid_ptr", "$oid_dispatch.oid_ptr"]},
        ]
    }
    field_bindings = {
        "oid_dispatch.cmp_bytes": "sizeof(DATA_OID)",
        "oid_dispatch.oid_bytes_written": "len + 1",
        "oid_decl.sizeof_oid": "sizeof(oid)",
        "oid_read.oid_bytes_written": "len + 1",
        "oid_read.oid_ptr": "oid",
        "oid_dispatch.oid_ptr": "oid",
    }
    return verified_invariants, verified_assertions, field_bindings


def test_annotate_marks_eq_identity_flow_as_connectivity_not_scored():
    vi, va, fb = _uninit_invariants()
    annotate_scored_invariants(vi, va, fb)
    edges = {e["invariant_id"]: e for e in vi["edges"]}
    # oid == oid is pure connectivity -> not a scored reasoning key
    assert edges["I-READ-TO-DISPATCH"]["scored"] is False
    assert edges["I-READ-TO-DISPATCH"]["scored_role"] == "connectivity"
    # sizeof(oid) > len + 1 is a real relation -> scored reasoning
    assert edges["I-DECL-TO-READ"]["scored"] is True
    assert edges["I-DECL-TO-READ"]["scored_role"] == "reasoning"


def test_build_assertion_reward_spec_projects_source_expressions():
    verified_assertions = {
        "schema_version": "verified-assertions-v3",
        "sample_id": "sample",
        "assertions": [
            {
                "id": "REQ.bounds",
                "kind": "required",
                "at": "append",
                "check": ["lt", "$append.index", "$append.capacity"],
            },
            {
                "id": "OBS.bad_index",
                "kind": "observed",
                "at": "read",
                "check": ["ge", "$read.index", "$read.zero_literal"],
            },
            {
                "id": "TR.index_flow",
                "kind": "transition",
                "from": "append",
                "at": "read",
                "check": ["eq", "$append.index", "$read.index"],
            },
        ],
    }
    field_bindings = {
        "append.index": "st->num_fields",
        "append.capacity": "MAX_FIELDS",
        "read.index": "i",
    }
    event_locations = {
        "append": {"file": "parser.c", "function": "parse", "line": 10},
        "read": {"file": "free.c", "function": "destroy", "line": 20},
    }
    ground_truth = {
        "source": {"file": "input.c", "function": "entry", "line": 5},
    }

    spec = build_assertion_reward_spec(
        verified_assertions, field_bindings, event_locations, ground_truth=ground_truth
    )

    assert spec["protocol"] == "assertion-reward-v1"
    assert spec["admission"][0]["at"]["file"] == "input.c"
    assert spec["claims"][0]["from"] is None
    assert spec["claims"][0]["check"] == {
        "op": "lt",
        "left": "st->num_fields",
        "right": "MAX_FIELDS",
    }
    assert spec["claims"][1]["check"]["right"] == "0"
    assert spec["claims"][2]["from"] == event_locations["append"]


def test_annotate_marks_observed_sink_inequality_as_mechanism():
    vi, va, fb = _uninit_invariants()
    annotate_scored_invariants(vi, va, fb)
    sink = vi["nodes"][0]
    assert sink["scored"] is True
    assert sink["scored_role"] == "mechanism"
    # the discriminative read-length > initialized-length relation, in source terms
    assert sink["relation"] == "sizeof(DATA_OID) > len + 1"


def test_binding_coverage_resolves_unqualified_operand_against_assertion_event():
    spec = {
        "assertions": [{
            "at": "sink",
            "check": ["gt", "$read_len", "$sink.init_len"],
        }]
    }
    coverage = validate_binding_coverage(
        spec,
        {
            "sink.read_len": "sizeof(marker)",
            "sink.init_len": "written + 1",
        },
        {"sink": {"function": "parse", "file": "parser.c"}},
    )

    assert coverage["errors"] == []
    assert coverage["warnings"] == []


def test_annotate_resolves_unqualified_operand_against_assertion_event():
    vi = {
        "nodes": [{
            "invariant_id": "I-SINK",
            "verified_by": "A-SINK",
        }],
        "edges": [],
    }
    va = {
        "assertions": [{
            "id": "A-SINK",
            "kind": "observed",
            "at": "sink",
            "check": ["gt", "$read_len", "$init_len"],
        }]
    }
    annotate_scored_invariants(
        vi,
        va,
        {
            "sink.read_len": "sizeof(marker)",
            "sink.init_len": "written + 1",
        },
    )

    assert vi["nodes"][0]["relation"] == "sizeof(marker) > written + 1"


def test_annotate_keeps_aliasing_eq_between_different_expressions_as_reasoning():
    # double-free: the two frees name the SAME object under DIFFERENT expressions;
    # asserting they alias is a real reasoning step, not connectivity.
    vi = {"nodes": [], "edges": [{"invariant_id": "E-ALIAS", "type": "data", "verified_by": "A-ALIAS"}]}
    va = {"assertions": [
        {"id": "A-ALIAS", "kind": "transition",
         "check": ["eq", "$first_free.free_argument", "$second_free.free_argument"]},
    ]}
    fb = {"first_free.free_argument": "cur->name", "second_free.free_argument": "id->name"}
    annotate_scored_invariants(vi, va, fb)
    assert vi["edges"][0]["scored"] is True
    assert vi["edges"][0]["scored_role"] == "reasoning"


def test_annotate_never_demotes_an_order_edge_even_on_a_single_object():
    # free-before-free of the SAME expression: the happens-before is the reasoning,
    # so an order edge is never collapsed to connectivity.
    vi = {"nodes": [], "edges": [{"invariant_id": "E-ORDER", "type": "order", "verified_by": "A-ORDER"}]}
    va = {"assertions": [
        {"id": "A-ORDER", "kind": "transition",
         "check": ["eq", "$first_free.ptr", "$second_free.ptr"]},
    ]}
    fb = {"first_free.ptr": "p", "second_free.ptr": "p"}
    annotate_scored_invariants(vi, va, fb)
    assert vi["edges"][0]["scored"] is True
    assert vi["edges"][0]["scored_role"] == "reasoning"


def test_assertion_cli_fails_generation_on_msan_offset_off_by_one(tmp_path, monkeypatch):
    # End-to-end: a uninitialized-value sample whose sink asserts read_len(7) > init_len(3)
    # while MSAN says the initialized prefix is only 2 bytes must fail generation.
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "msan_sample",
        "original_case": "orig",
        "assertions": [
            {
                "id": "A-SINK",
                "kind": "observed",
                "at": "sink",
                "check": ["gt", "$sink.read_len", "$sink.init_len"],
                "invariants": ["I-SINK"],
            }
        ],
    }
    spec["content_hash"] = assertion_content_hash(spec)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    trace = "CASE name=orig rc=1 result=crash\nASSERT_EVT point=sink read_len=7 init_len=3\nENDCASE\n"
    (tmp_path / "vul.txt").write_text(trace)
    (tmp_path / "fix.txt").write_text(trace)
    (tmp_path / "san.txt").write_text(
        "==7==WARNING: MemorySanitizer: use-of-uninitialized-value\n"
        "Uninitialized bytes in __interceptor_memcmp at offset 2 inside [0x7ffff000, 7)\n"
    )
    results_path = tmp_path / "results.json"
    monkeypatch.setattr(sys, "argv", [
        "assertions",
        "--spec", str(spec_path),
        "--vulnerable-trace", str(tmp_path / "vul.txt"),
        "--fixed-trace", str(tmp_path / "fix.txt"),
        "--sanitizer-trace", str(tmp_path / "san.txt"),
        "--results-out", str(results_path),
    ])
    with pytest.raises(SystemExit) as exc:
        assertions_module.main()
    assert exc.value.code == 1
    written = json.loads(results_path.read_text())
    assert written["differential_status"] == "not_applicable"
    assert written["msan_offset_check"]["matched"] is False
    assert "off-by-one" in written["msan_offset_check"]["error"]


def test_freeze_marker_commits_exact_spec_bytes(tmp_path):
    spec, _, _ = _inputs()
    spec_path = tmp_path / "candidate_assertions.json"
    marker_path = tmp_path / ".assertion_spec_frozen.json"
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")

    marker = freeze_spec(spec_path, marker_path)

    assert marker["content_hash"] == spec["content_hash"]
    assert marker["spec_path"] == str(spec_path.resolve())
    assert json.loads(marker_path.read_text()) == marker


def test_observed_assertions_reject_runtime_literal_answers():
    spec, _, _ = _inputs()
    spec["assertions"][0]["check"] = ["eq", "$stored_count", -2147483648]
    spec["content_hash"] = assertion_content_hash(spec)
    with pytest.raises(ValueError, match="runtime literals"):
        validate_frozen_spec(spec)


def test_binding_uses_one_direction_without_assertion_ids_in_invariants():
    spec = _minimal_v3_spec([
        {
            "id": "observed.materialization",
            "kind": "observed",
            "at": "materialize",
            "check": ["eq", "$materialize.stored_count", "$materialize.source_count"],
            "invariants": ["node.materialization"],
        },
        {
            "id": "observed.normalization",
            "kind": "observed",
            "at": "read",
            "check": ["eq", "$read.read_count", "$read.normalized_count"],
            "invariants": ["node.normalization"],
        },
        {
            "id": "observed.read_size",
            "kind": "observed",
            "at": "read",
            "check": ["gt", "$read.requested_bytes", "$read.destination_bytes"],
            "invariants": ["node.read_size"],
        },
        {
            "id": "transition.count_dataflow",
            "kind": "transition",
            "from": "source",
            "at": "materialize",
            "check": ["eq", "$source.source_count", "$materialize.stored_count"],
            "invariants": ["edge.count_dataflow"],
        },
        {
            "id": "required.raw_count_lower_bound",
            "kind": "required",
            "at": "range_decision",
            "check": ["ge", "$range_decision.raw_count", "$range_decision.lower_bound"],
            "invariants": ["root_cause_criterion"],
        },
    ])
    verified_invariants = {
        "nodes": [
            _contract_node("node.source", "source", ["source_count"]),
            _contract_node("node.materialization", "intermediate", ["stored_count"]),
            _contract_node("node.normalization", "intermediate", ["normalized_count"]),
            _contract_node(
                "node.read_size",
                "sink",
                ["requested_bytes", "destination_bytes"],
                {"op": "gt", "left": "requested_bytes", "right": "destination_bytes"},
            ),
            _root_node(),
        ],
        "edges": [
            _contract_edge(
                "edge.count_dataflow",
                "node.source",
                "node.materialization",
                ["source_count", "stored_count"],
            )
        ],
        "root_cause_criterion": {"invariant_id": "root_cause_criterion"},
    }

    assert validate_invariant_bindings(verified_invariants, spec)["valid"] is True
    spec["assertions"][0]["invariants"] = ["unknown"]
    assert validate_invariant_bindings(verified_invariants, spec)["valid"] is False


def test_every_selected_reasoning_invariant_requires_assertion_coverage():
    spec = _minimal_v3_spec([
        {
            "id": "observed.static_anchor",
            "kind": "observed",
            "at": "post_read",
            "check": ["ne", "$post_read.before", "$post_read.after"],
            "invariants": ["node.static_anchor"],
        },
        {
            "id": "transition.dynamic",
            "kind": "transition",
            "from": "post_read",
            "at": "decision",
            "check": ["eq", "$post_read.dynamic_value", "$decision.dynamic_value"],
            "invariants": ["edge.dynamic"],
        },
        {
            "id": "required.root",
            "kind": "required",
            "at": "decision",
            "check": ["ge", "$decision.raw_count", "$decision.lower_bound"],
            "invariants": ["root"],
        },
    ])
    verified_invariants = {
        "nodes": [
            _contract_node("node.static_anchor", "intermediate", ["static_anchor"]),
            _root_node("root"),
        ],
        "edges": [
            _contract_edge("edge.dynamic", "node.static_anchor", "root", ["dynamic_value"])
        ],
        "root_cause_criterion": {"invariant_id": "root"},
    }
    assert validate_invariant_bindings(verified_invariants, spec)["valid"] is True
    spec["assertions"] = [spec["assertions"][-1]]
    spec["content_hash"] = assertion_content_hash(spec)
    result = validate_invariant_bindings(verified_invariants, spec)
    assert result["valid"] is False
    assert "invariant edge.dynamic has no assertion" in result["errors"]
    assert "invariant node.static_anchor has no assertion" in result["errors"]
    assert (
        "edge edge.dynamic requires exactly one transition assertion; got []"
        in result["errors"]
    )


def test_verified_invariant_file_rejects_false_null_and_refuted_entries():
    spec, _, _ = _inputs()
    data = {
        "nodes": [{
            "invariant_id": "node.materialization",
            "role": "intermediate",
            "operands": ["stored_count"],
            "relation": {"op": "same_object", "left": "stored_count", "right": "stored_count"},
            "verified": None,
        }],
        "edges": [{
            "invariant_id": "edge.count_dataflow",
            "type": "data",
            "from_node": "node.materialization",
            "to_node": "root_cause_criterion",
            "operands": ["stored_count"],
            "relation": {"op": "same_object", "left": "stored_count", "right": "stored_count"},
            "verified": False,
        }],
        "root_cause_criterion": {
            "invariant_id": "root_cause_criterion",
        },
        "refuted": [],
    }
    result = validate_invariant_bindings(data, spec)
    assert result["valid"] is False
    assert "verified_invariants.json must not contain a refuted section" in result["errors"]
    assert "node invariant node.materialization is not verified" in result["errors"]
    assert "edge invariant edge.count_dataflow is not verified" in result["errors"]


def test_binding_allows_unverified_propagation_subset_but_keeps_root_required():
    spec = _minimal_v3_spec([
        {
            "id": "observed.read_size",
            "kind": "observed",
            "at": "read",
            "check": ["gt", "$read.requested_bytes", "$read.destination_bytes"],
            "invariants": ["node.read_size"],
        },
        {
            "id": "required.raw_count_lower_bound",
            "kind": "required",
            "at": "range_decision",
            "check": ["ge", "$range_decision.raw_count", "$range_decision.lower_bound"],
            "invariants": ["root_cause_criterion"],
        },
    ])
    data = {
        "nodes": [
            _contract_node("node.materialization", "source", ["stored_count"]),
            _root_node(),
            _contract_node(
                "node.read_size",
                "sink",
                ["requested_bytes", "destination_bytes"],
                {"op": "gt", "left": "requested_bytes", "right": "destination_bytes"},
            ),
        ],
        "edges": [
            {
                **_contract_edge(
                    "edge.count_dataflow",
                    "node.materialization",
                    "node.read_size",
                    ["stored_count"],
                ),
                "invariant_id": "edge.count_dataflow",
                "verified": False,
                "verification_status": "omitted_not_runtime_verified",
            },
        ],
        "root_cause_criterion": {
            "invariant_id": "root_cause_criterion",
        },
    }

    result = validate_invariant_bindings(data, spec)

    assert result["valid"] is True
    assert result["skipped_unverified"] == ["edge.count_dataflow"]


def test_binding_rejects_required_assertion_when_root_is_filtered_out():
    spec = _minimal_v3_spec([
        {
            "id": "required.raw_count_lower_bound",
            "kind": "required",
            "at": "range_decision",
            "check": ["ge", "$range_decision.raw_count", "$range_decision.lower_bound"],
            "invariants": ["root_cause_criterion"],
        },
    ])
    root = _contract_node("root_cause_criterion", "root_cause", ["raw_count"])
    root.pop("relation")
    data = {
        "nodes": [
            root,
        ],
        "edges": [],
        "root_cause_criterion": {
            "invariant_id": "root_cause_criterion",
        },
    }

    result = validate_invariant_bindings(data, spec)

    assert result["valid"] is False
    assert (
        "node invariant root_cause_criterion missing relation"
    ) in result["errors"]


def test_build_verified_invariants_preserves_source_identity_and_edge_endpoints():
    candidate = {
        "schema_version": "legacy-must-be-dropped",
        "sample_id": "sample",
        "nodes": [
            _contract_node("node.source", "source", ["input"]),
            _root_node("node.root"),
            _contract_node("node.sink", "sink", ["dst"]),
            _contract_node("node.unverified", "intermediate", ["tmp"]),
        ],
        "edges": [
            _contract_edge("edge.root_to_sink", "node.root", "node.sink", ["dst"]),
            _contract_edge("edge.unverified", "node.source", "node.unverified", ["tmp"]),
        ],
        "root_cause_criterion": {
            "invariant_id": "node.root",
            "file": "src/old.c",
            "line": 99,
        },
    }
    results = {
        "assertions": [
            {"id": "required.root", "kind": "required", "verified": True, "invariants": ["node.root"]},
            {
                "id": "transition.root_to_sink",
                "kind": "transition",
                "verified": True,
                "invariants": ["edge.root_to_sink"],
            },
            {
                "id": "transition.unverified",
                "kind": "transition",
                "verified": False,
                "invariants": ["edge.unverified"],
            },
        ]
    }

    verified = build_verified_invariants(candidate, results)

    assert "schema_version" not in verified
    assert verified["root_cause_criterion"] == {"invariant_id": "node.root"}
    assert {node["invariant_id"] for node in verified["nodes"]} == {
        "node.source",
        "node.root",
        "node.sink",
    }
    assert [edge["invariant_id"] for edge in verified["edges"]] == ["edge.root_to_sink"]


def test_v3_transition_assertion_verifies_ordered_cross_event_value_flow():
    assertion = {
        "id": "transition.label_to_free",
        "kind": "transition",
        "from": "post_read",
        "at": "free_sink",
        "check": ["eq", "$post_read.label_after", "$free_sink.free_argument"],
        "invariants": ["edge.label_to_free"],
    }
    case = {
        "events": [
            {"point": "post_read", "label_after": 0x202020},
            {"point": "free_sink", "free_argument": 0x202020},
        ]
    }

    result = evaluate_assertion(assertion, case, "vulnerable")

    assert result["status"] == "satisfied"
    assert result["ordered"] is True
    assert result["from_index"] == 0
    assert result["to_index"] == 1


def test_v3_transition_assertion_rejects_reversed_event_order():
    assertion = {
        "id": "transition.label_to_free",
        "kind": "transition",
        "from": "post_read",
        "at": "free_sink",
        "check": ["eq", "$post_read.label_after", "$free_sink.free_argument"],
        "invariants": ["edge.label_to_free"],
    }
    case = {
        "events": [
            {"point": "free_sink", "free_argument": 0x202020},
            {"point": "post_read", "label_after": 0x202020},
        ]
    }

    result = evaluate_assertion(assertion, case, "vulnerable")

    assert result["status"] == "out_of_order"
    assert result["satisfied"] is False


def test_v3_transition_binds_the_latest_source_before_the_target():
    assertion = {
        "id": "transition.value_flow",
        "kind": "transition",
        "from": "source",
        "at": "sink",
        "check": ["eq", "$source.value", "$sink.value"],
        "invariants": ["edge.value_flow"],
    }
    case = {
        "events": [
            {"point": "source", "value": 1},
            {"point": "sink", "value": 1},
            {"point": "source", "value": 2},
            {"point": "sink", "value": 2},
            {"point": "source", "value": 3},
        ]
    }

    result = evaluate_assertion(assertion, case, "vulnerable")

    assert result["status"] == "satisfied"
    assert result["left"] == result["right"] == 2
    assert result["from_index"] == 2
    assert result["to_index"] == 3


def test_v3_transition_must_compare_fields_from_both_endpoints():
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [
            {
                "id": "transition.fake_edge",
                "kind": "transition",
                "from": "source",
                "at": "sink",
                "check": ["eq", "$sink.before", "$sink.after"],
                "invariants": ["edge.value_flow"],
            }
        ],
    }
    spec["content_hash"] = assertion_content_hash(spec)

    with pytest.raises(ValueError, match="directly relate"):
        validate_frozen_spec(spec)


def test_v3_binding_requires_one_dedicated_transition_per_edge():
    invariants = {
        "nodes": [
            _contract_node("node.source", "source", ["input"]),
            _contract_node(
                "node.corrupted",
                "intermediate",
                ["label_before", "label_after"],
                {"op": "ne", "left": "label_before", "right": "label_after"},
            ),
            _contract_node("node.sink", "sink", ["free_argument"]),
            _root_node("root.required"),
        ],
        "edges": [
            _contract_edge(
                "edge.corruption_to_sink",
                "node.corrupted",
                "node.sink",
                ["label_after", "free_argument"],
            ),
        ],
        "root_cause_criterion": {"invariant_id": "root.required"},
    }
    assertions = [
        {
            "id": "observed.corrupted",
            "kind": "observed",
            "at": "post_read",
            "check": ["ne", "$label_after", "$label_before"],
            "invariants": ["node.corrupted"],
        },
        {
            "id": "transition.corruption_to_sink",
            "kind": "transition",
            "from": "post_read",
            "at": "free_sink",
            "check": ["eq", "$post_read.label_after", "$free_sink.free_argument"],
            "invariants": ["edge.corruption_to_sink", "node.sink"],
        },
        {
            "id": "required.root",
            "kind": "required",
            "at": "decision",
            "check": ["ge", "$count", -3],
            "invariants": ["root.required"],
        },
    ]
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": assertions,
    }
    spec["content_hash"] = assertion_content_hash(spec)

    assert validate_invariant_bindings(invariants, spec)["valid"] is True

    assertions[1]["kind"] = "observed"
    assertions[1].pop("from")
    spec["content_hash"] = assertion_content_hash(spec)
    result = validate_invariant_bindings(invariants, spec)
    assert result["valid"] is False
    assert any("non-transition assertion" in error for error in result["errors"])
    assert any("requires exactly one transition assertion" in error for error in result["errors"])


def test_v3_transition_cannot_claim_multiple_edges():
    invariants = {
        "nodes": [
            _contract_node("node.source", "source", ["value"]),
            _root_node("node.root"),
            _contract_node("node.sink", "sink", ["value"]),
        ],
        "edges": [
            _contract_edge("edge.one", "node.source", "node.root", ["value"]),
            _contract_edge("edge.two", "node.root", "node.sink", ["value"]),
        ],
        "root_cause_criterion": {"invariant_id": "node.root"},
    }
    assertion = {
        "id": "transition.overclaimed",
        "kind": "transition",
        "from": "source",
        "at": "sink",
        "check": ["eq", "$source.value", "$sink.value"],
        "invariants": ["edge.one", "edge.two"],
    }
    spec = {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "original_case": "original",
        "assertions": [assertion],
    }
    spec["content_hash"] = assertion_content_hash(spec)

    result = validate_invariant_bindings(invariants, spec)

    assert result["valid"] is False
    assert any("must cover exactly one edge" in error for error in result["errors"])
