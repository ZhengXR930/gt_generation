import json
import sys
from pathlib import Path

import pytest
from gt_generation.gt_toolkit import assertions as assertions_module

from gt_generation.gt_toolkit.assertions import (
    assertion_content_hash,
    build_perturbation_results,
    build_verified_assertions,
    evaluate_assertion,
    freeze_spec,
    parse_trace_matrix,
    validate_assertions,
    validate_frozen_spec,
    validate_invariant_bindings,
)
from reasoning.questions import (
    build_question_request,
    finalize_questions,
    render_with_agent,
)


FIXTURE = Path(__file__).parent / "fixtures" / "assertion_pilot_12595"


def _inputs():
    spec = json.loads((FIXTURE / "candidate_assertions.json").read_text())
    vulnerable = parse_trace_matrix((FIXTURE / "vulnerable_trace.txt").read_text())
    fixed = parse_trace_matrix((FIXTURE / "fixed_trace.txt").read_text())
    return spec, vulnerable, fixed


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


def test_frozen_hash_detects_post_execution_rewrite():
    spec, _, _ = _inputs()
    assert spec["content_hash"] == assertion_content_hash(spec)
    validate_frozen_spec(spec)

    spec["assertions"][0]["check"] = ["ne", "$stored_count", "$source_count"]
    with pytest.raises(ValueError, match="content hash mismatch"):
        validate_frozen_spec(spec)


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
    spec, _, _ = _inputs()
    verified_invariants = {
        "nodes": [
            {"invariant_id": "node.materialization"},
            {"invariant_id": "node.normalization"},
            {"invariant_id": "node.read_size"},
        ],
        "edges": [{"invariant_id": "edge.count_dataflow"}],
        "root_cause_criterion": {"invariant_id": "root_cause_criterion"},
    }

    assert validate_invariant_bindings(verified_invariants, spec)["valid"] is True
    spec["assertions"][0]["invariants"] = ["unknown"]
    assert validate_invariant_bindings(verified_invariants, spec)["valid"] is False


def test_every_selected_reasoning_invariant_requires_assertion_coverage():
    spec, _, _ = _inputs()
    verified_invariants = {
        "nodes": [{"invariant_id": "node.static_anchor", "verified": True}],
        "edges": [{"invariant_id": "edge.dynamic", "verified": True}],
        "root_cause_criterion": {"invariant_id": "root", "verified": True},
    }
    spec["assertions"] = [
        {**spec["assertions"][0], "invariants": ["node.static_anchor", "edge.dynamic"]},
        {**spec["assertions"][-1], "invariants": ["root"]},
    ]
    assert validate_invariant_bindings(verified_invariants, spec)["valid"] is True
    spec["assertions"] = spec["assertions"][1:]
    result = validate_invariant_bindings(verified_invariants, spec)
    assert result["valid"] is False
    assert result["errors"] == [
        "invariant edge.dynamic has no assertion",
        "invariant node.static_anchor has no assertion",
    ]


def test_verified_invariant_file_rejects_false_null_and_refuted_entries():
    spec, _, _ = _inputs()
    data = {
        "nodes": [{
            "invariant_id": "node.materialization",
            "verified": None,
        }],
        "edges": [{
            "invariant_id": "edge.count_dataflow",
            "verified": False,
        }],
        "root_cause_criterion": {
            "invariant_id": "root_cause_criterion",
            "verified": True,
        },
        "refuted": [],
    }
    result = validate_invariant_bindings(data, spec)
    assert result["valid"] is False
    assert "verified_invariants.json must not contain a refuted section" in result["errors"]
    assert "node invariant node.materialization is not verified" in result["errors"]
    assert "edge invariant edge.count_dataflow is not verified" in result["errors"]


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
            {"invariant_id": "node.corrupted", "verified": True},
            {"invariant_id": "node.sink", "verified": True},
        ],
        "edges": [
            {"invariant_id": "edge.corruption_to_sink", "verified": True},
        ],
        "root_cause_criterion": {"invariant_id": "root.required", "verified": True},
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
        "edges": [
            {"invariant_id": "edge.one", "verified": True},
            {"invariant_id": "edge.two", "verified": True},
        ]
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


def _question_inputs():
    verified_assertions = {
        "schema_version": "verified-assertions-v3",
        "sample_id": "sample",
        "assertions": [
            {
                "id": "required.range",
                "kind": "required",
                "at": "decision",
                "check": ["ge", "$raw_count", -3],
                "invariants": ["root.range"],
            },
            {
                "id": "transition.source_to_mid",
                "kind": "transition",
                "from": "load",
                "at": "normalize",
                "check": ["eq", "$load.value", "$normalize.input"],
                "invariants": ["edge.source_to_mid"],
            },
            {
                "id": "transition.mid_to_sink",
                "kind": "transition",
                "from": "normalize",
                "at": "free_sink",
                "check": ["eq", "$normalize.output", "$free_sink.argument"],
                "invariants": ["edge.mid_to_sink"],
            },
        ],
    }
    verified_invariants = {
        "nodes": [
            {"invariant_id": "node.source", "role": "input", "verified": True},
            {"invariant_id": "node.mid", "role": "state", "verified": True},
            {"invariant_id": "node.sink", "role": "sink", "verified": True},
        ],
        "edges": [
            {
                "invariant_id": "edge.source_to_mid",
                "from": "node.source",
                "to": "node.mid",
                "verified": True,
            },
            {
                "invariant_id": "edge.mid_to_sink",
                "from": "node.mid",
                "to": "node.sink",
                "verified": True,
            },
        ],
        "root_cause_criterion": {
            "invariant_id": "root.range",
            "variable": "raw_count",
            "verified": True,
        },
    }
    ground_truth = {
        "sink": {
            "code": "free(info->label);",
            "function": "release_info",
            "file": "parser.c",
        }
    }
    reachability = {
        "R3_sink_function_reached": True,
        "R4_sink_line_reached": True,
        "R5_sanitizer_triggered": True,
    }
    return verified_assertions, verified_invariants, ground_truth, reachability, "ASAN bad free"


def test_questioning_agent_selects_exactly_three_verified_dimensions():
    inputs = _question_inputs()

    request = build_question_request(*inputs)

    assert [item["id"] for item in request["items"]] == [
        "reach",
        "mechanism",
        "propagation",
    ]
    assert all(set(item) == {"id", "statement", "context"} for item in request["items"])

    output = finalize_questions(
        *inputs,
        {"questions": [
            {"id": "reach", "question": "Which exact operation is the final sink? ___"},
            {"id": "mechanism", "question": "What condition is required at decision? ___"},
            {
                "id": "propagation",
                "question": "Complete the relation from load to normalize ___P1___ and from normalize to the sink ___P2___",
            },
        ]},
    )

    assert [item["dimension"] for item in output["probes"]] == [
        "Reach",
        "Mechanism",
        "Propagation",
    ]
    assert output["probes"][0]["answer"] == "free(info->label)"
    assert output["probes"][1]["answer"] == "raw_count >= -3"
    assert output["probes"][2]["answer"] == (
        "load.value == normalize.input -> normalize.output == free_sink.argument"
    )
    assert output["probes"][2]["assertions"] == [
        "transition.source_to_mid",
        "transition.mid_to_sink",
    ]


def test_questioning_agent_subprocess_contract_is_minimal(tmp_path):
    inputs = _question_inputs()
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import argparse, json\n"
        "p=argparse.ArgumentParser(); p.add_argument('--role-file'); "
        "p.add_argument('--input'); p.add_argument('--output'); a=p.parse_args()\n"
        "items=json.load(open(a.input))['items']\n"
        "questions=[{'id': x['id'], 'question': "
        "('Complete: ___P1___ then ___P2___' if x['id']=='propagation' else "
        "'Recover the complete code-specific expression: ___')} for x in items]\n"
        "json.dump({'questions': questions}, open(a.output, 'w'))\n",
        encoding="utf-8",
    )
    role = tmp_path / "role.md"
    role.write_text("render questions", encoding="utf-8")

    output = render_with_agent(
        verified_assertions=inputs[0],
        verified_invariants=inputs[1],
        ground_truth=inputs[2],
        reachability=inputs[3],
        public_text=inputs[4],
        agent_command=f"{sys.executable} {adapter}",
        role_file=role,
    )

    assert [item["id"] for item in output["probes"]] == [
        "reach",
        "mechanism",
        "propagation",
    ]


def test_question_renderer_rejects_answer_form_hints():
    inputs = _question_inputs()
    request = build_question_request(*inputs)
    rendered = {"questions": [
        {
            "id": item["id"],
            "question": (
                "Recover: ___P1___ then ___P2___"
                if item["id"] == "propagation"
                else "Recover the source expression: ___"
            ),
        }
        for item in request["items"]
    ]}
    rendered["questions"][1]["question"] = "What lower-bound relation holds? ___"
    with pytest.raises(ValueError, match="answer-form hint"):
        finalize_questions(*inputs, rendered)


def test_probe_selection_rejects_publicly_explicit_gold():
    inputs = list(_question_inputs())
    inputs[4] = "The crash occurs at free(info->label)."

    with pytest.raises(ValueError, match="Reach unavailable"):
        build_question_request(*inputs)


def test_probe_selection_treats_explicit_violation_condition_as_mechanism_leak():
    inputs = list(_question_inputs())
    inputs[4] = "The missing validation is raw_count < -3."

    with pytest.raises(ValueError, match="Mechanism unavailable"):
        build_question_request(*inputs)
