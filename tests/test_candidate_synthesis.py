import json
import os
from pathlib import Path

from candidate_synthesis_core import (
    build_candidate,
    lookup_format_memory,
    record_candidate_plan,
    record_construction_support_request,
)
from candidate_synthesis_core.core import (
    _agent_hypothesis_feedback,
    _coverage_diagnostics_from_export,
    _evaluate_h1_h5,
)


def test_on_demand_construction_support_flow(tmp_path: Path) -> None:
    state = tmp_path / "reasoning_state.json"
    state.write_text(
        json.dumps(
            {
                "primary_source": {"file": "parser.c", "function": "parse", "line": 10, "event_id": 1},
                "primary_root_cause": {"file": "parser.c", "function": "parse", "line": 20, "event_id": 1},
                "primary_sink": {"file": "parser.c", "function": "parse", "line": 30, "event_id": 1},
                "trace": [
                    {
                        "step": 1,
                        "from": "input",
                        "to": "len",
                        "relation": "parse",
                        "file": "parser.c",
                        "function": "parse",
                        "line": 10,
                        "event_id": 1,
                    }
                ],
                "next_missing": [],
                "snapshot": {"event_id": 1, "note": "toy parser bug"},
            }
        ),
        encoding="utf-8",
    )

    request = {
        "reasoning_event_ids": [1],
        "input_modality": "file_bytes",
        "format_or_protocol": "toy-container",
        "construction_goal": "Build a structurally valid toy container reaching parse().",
        "retrieval_plan": {
            "needed": True,
            "purpose": "Use public format docs only if the agent cannot construct it directly.",
        },
        "allowed_sources": ["format documentation"],
        "disallowed_sources": ["target PoC"],
        "builder_interface": {
            "kind": "external_command",
            "output": "candidate file at {candidate}",
        },
    }

    rejected = record_construction_support_request(
        request={**request, "reasoning_event_ids": []},
        workspace=tmp_path,
    )
    assert rejected["accepted"] is False

    support = record_construction_support_request(
        request=request,
        workspace=tmp_path,
        reasoning_state_path=state,
    )
    assert support["accepted"] is True
    assert support["support_id"] == "support_0001"

    builder = tmp_path / "builder.py"
    builder.write_text(
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_bytes(b'TOY')\n",
        encoding="utf-8",
    )
    os.chmod(builder, 0o755)

    plan = record_candidate_plan(
        plan={
            "reasoning_event_ids": [1],
            "construction_support_ids": [support["support_id"]],
            "hypothesis": {"summary": "toy container reaches parser"},
            "target_input_component": {"format": "toy-container"},
            "construction_strategy": {"mode": "external_builder"},
            "builder": {
                "kind": "external_command",
                "command": "python3 builder.py {candidate}",
            },
            "expected_effect": {"delivery": "candidate is non-empty"},
        },
        workspace=tmp_path,
        reasoning_state_path=state,
    )
    assert plan["accepted"] is True
    assert plan["plan"]["reasoning_complete"] is True
    assert plan["plan"]["reasoning_state_snapshot"]["primary_source"]["line"] == 10

    candidate = build_candidate(plan_id=plan["plan_id"], workspace=tmp_path)
    assert candidate["built"] is True
    assert candidate["construction_support_ids"] == [support["support_id"]]
    assert candidate["reasoning_complete"] is True
    assert candidate["reasoning_state_snapshot"]["primary_root_cause"]["line"] == 20
    assert Path(candidate["candidate_path"]).read_bytes() == b"TOY"


def test_lookup_format_memory_returns_generic_rar5_notes() -> None:
    result = lookup_format_memory(
        format_or_protocol="RAR5",
        query="need valid archive that reaches compressed block parsing",
    )

    assert result["matches"]
    match = result["matches"][0]
    assert match["memory_id"].startswith("format.rarfive")
    assert match["provenance"]["target_specific"] is False
    assert match["provenance"]["contains_poc"] is False
    assert any("signature bytes 52 61 72 21" in note for note in match["construction_notes"])
    assert match["builder_guidance"]["positive_output_contract"]["required_prefix_hex"] == "526172211a070100"


def test_support_request_attaches_external_format_memory(tmp_path: Path) -> None:
    state = tmp_path / "reasoning_state.json"
    state.write_text(
        json.dumps(
            {
                "primary_source": {"file": "rar.c", "function": "parse_tables", "line": 2002},
                "primary_root_cause": {"file": "rar.c", "function": "parse_tables", "line": 2027},
                "primary_sink": {"file": "rar.c", "function": "parse_tables", "line": 2028},
                "trace": [{"step": 1, "from": "block", "to": "table", "relation": "parse"}],
                "next_missing": [],
                "snapshot": {"event_id": 1, "note": "rar parser bug"},
            }
        ),
        encoding="utf-8",
    )

    support = record_construction_support_request(
        request={
            "input_modality": "raw file",
            "format_or_protocol": "RAR5",
            "construction_goal": "Reach compressed block parsing in a valid RAR5 archive.",
            "needed_knowledge": ["base block and compressed block structure"],
            "builder_interface": {"kind": "external_command", "output": "candidate at {candidate_path}"},
        },
        workspace=tmp_path,
        reasoning_state_path=state,
    )

    assert support["accepted"] is True
    matches = support["external_memory"]["matches"]
    assert matches
    assert matches[0]["scope"] == "generic_format_construction"


def test_candidate_plan_requires_complete_reasoning_snapshot(tmp_path: Path) -> None:
    state = tmp_path / "reasoning_state.json"
    state.write_text(
        json.dumps(
            {
                "primary_source": {"file": "parser.c", "function": "parse", "line": 10},
                "next_missing": ["root_cause", "edge", "sink"],
            }
        ),
        encoding="utf-8",
    )

    plan = record_candidate_plan(
        plan={
            "hypothesis": {"summary": "incomplete"},
            "target_input_component": {"format": "toy-container"},
            "construction_strategy": {"mode": "external_builder"},
            "builder": {
                "kind": "external_command",
                "command": "python3 builder.py {candidate}",
            },
            "expected_effect": {"delivery": "candidate is non-empty"},
        },
        workspace=tmp_path,
        reasoning_state_path=state,
    )

    assert plan["accepted"] is False
    assert any("minimal bound vulnerability hypothesis" in err for err in plan["errors"])


def test_h1_feedback_includes_coverage_context_without_new_stage(tmp_path: Path) -> None:
    coverage = {
        "data": [
            {
                "files": [
                    {
                        "filename": "/src/project/parser.c",
                        "segments": [
                            [10, 1, 1, True, True],
                            [20, 1, 1, True, True],
                            [90, 1, 1, True, True],
                        ],
                    }
                ],
                "functions": [
                    {
                        "name": "parse_header",
                        "count": 1,
                        "filenames": ["/src/project/parser.c"],
                        "regions": [[1, 1, 25, 2, 1, 0, 0, 0]],
                    },
                    {
                        "name": "cleanup",
                        "count": 1,
                        "filenames": ["/src/project/parser.c"],
                        "regions": [[80, 1, 95, 2, 1, 0, 0, 0]],
                    },
                ],
            }
        ],
        "type": "llvm.coverage.json.export",
        "version": "2.0.1",
    }
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    source = tmp_path / "project/parser.c"
    source.parent.mkdir(parents=True)
    source.write_text("\n".join(f"line {line}" for line in range(1, 121)), encoding="utf-8")
    checkpoints = [
        {
            "kind": "claimed_source",
            "file": "project/parser.c",
            "function": "parse_deep",
            "line": 100,
            "code": "value = input[i];",
        }
    ]

    diagnostics = _coverage_diagnostics_from_export(
        coverage_path=coverage_path,
        checkpoints=checkpoints,
    )
    report = _evaluate_h1_h5(
        reasoning_state={"selected_event_id": 1},
        checkpoints=checkpoints,
        hits=[],
        sanitizer_trace="",
        coverage_diagnostics=diagnostics,
    )
    feedback = _agent_hypothesis_feedback(report)

    assert feedback["stage"] == "H1"
    assert feedback["failure_stage"] == "claimed_parser_not_reached"
    assert "First missed checkpoint" in feedback["message"]
    assert "Coverage reached project/parser.c but not line 100" in feedback["message"]
    assert "nearest covered line 90 in cleanup" in feedback["message"]
    assert "`line 90`" in feedback["message"]
    assert "do not resubmit the same structural candidate" in feedback["next_plan_requirement"]


def test_repeated_h1_rejects_direct_scratch_plan_without_seed(tmp_path: Path) -> None:
    state = tmp_path / "reasoning_state.json"
    state.write_text(
        json.dumps(
            {
                "primary_source": {"file": "parser.c", "function": "parse", "line": 10},
                "primary_root_cause": {"file": "parser.c", "function": "parse", "line": 20},
                "primary_sink": {"file": "parser.c", "function": "parse", "line": 30},
                "trace": [
                    {
                        "step": 1,
                        "from": "input",
                        "to": "sink",
                        "relation": "parse",
                        "file": "parser.c",
                        "function": "parse",
                        "line": 10,
                    }
                ],
                "next_missing": [],
                "snapshot": {"event_id": 1, "note": "deep parser bug"},
            }
        ),
        encoding="utf-8",
    )
    submissions = tmp_path / "candidate_submissions.jsonl"
    with submissions.open("w", encoding="utf-8") as handle:
        for index in range(2):
            handle.write(
                json.dumps(
                    {
                        "submit_id": f"submit_{index + 1:04d}",
                        "candidate_id": f"cand_{index + 1:04d}",
                        "plan_id": f"plan_{index + 1:04d}",
                        "construction_feedback": {
                            "stage": "H1",
                            "failure_stage": "claimed_parser_not_reached",
                            "message": "Candidate did not reach the recorded source checkpoint.",
                        },
                    }
                )
                + "\n"
            )

    rejected = record_candidate_plan(
        plan={
            "hypothesis": {"summary": "deep parser bug"},
            "target_input_component": {"description": "construct from scratch"},
            "construction_strategy": {"mode": "direct"},
            "builder": {
                "kind": "external_command",
                "command": "python3 -c 'open(__import__(\"sys\").argv[1],\"wb\").write(b\"X\")' {candidate_path}",
            },
            "expected_effect": {"description": "reach parser"},
            "previous_feedback": {"stage": "H1", "response": "will address H1"},
        },
        workspace=tmp_path,
        reasoning_state_path=state,
    )
    assert rejected["accepted"] is False
    assert any("positive output evidence" in error for error in rejected["errors"])

    accepted = record_candidate_plan(
        plan={
            "hypothesis": {"summary": "deep parser bug"},
            "target_input_component": {"description": "mutate known-valid seed"},
            "construction_strategy": {"mode": "seed_mutation"},
            "seed": {"path": "/workspace/seeds/minimal.bin"},
            "edits": [{"op": "append_bytes", "hex": "00"}],
            "expected_effect": {"description": "reach parser"},
            "previous_feedback": {"stage": "H1", "response": "switch to seed mutation"},
        },
        workspace=tmp_path,
        reasoning_state_path=state,
    )
    assert accepted["accepted"] is True


def test_repeated_h1_external_builder_requires_output_contract(tmp_path: Path) -> None:
    state = tmp_path / "reasoning_state.json"
    state.write_text(
        json.dumps(
            {
                "primary_source": {"file": "parser.c", "function": "parse", "line": 10},
                "primary_root_cause": {"file": "parser.c", "function": "parse", "line": 20},
                "primary_sink": {"file": "parser.c", "function": "parse", "line": 30},
                "trace": [
                    {
                        "step": 1,
                        "from": "input",
                        "to": "sink",
                        "relation": "parse",
                        "file": "parser.c",
                        "function": "parse",
                        "line": 10,
                    }
                ],
                "next_missing": [],
                "snapshot": {"event_id": 1, "note": "deep parser bug"},
            }
        ),
        encoding="utf-8",
    )
    submissions = tmp_path / "candidate_submissions.jsonl"
    with submissions.open("w", encoding="utf-8") as handle:
        for index in range(2):
            handle.write(
                json.dumps(
                    {
                        "submit_id": f"submit_{index + 1:04d}",
                        "candidate_id": f"cand_{index + 1:04d}",
                        "plan_id": f"plan_{index + 1:04d}",
                        "construction_feedback": {
                            "stage": "H1",
                            "failure_stage": "claimed_parser_not_reached",
                            "message": "Candidate did not reach the recorded source checkpoint.",
                        },
                    }
                )
                + "\n"
            )

    rejected = record_candidate_plan(
        plan={
            "hypothesis": {"summary": "deep parser bug"},
            "target_input_component": {"description": "external scratch builder"},
            "construction_strategy": {"mode": "external_builder"},
            "builder": {
                "kind": "external_command",
                "command": "python3 -c 'open(__import__(\"sys\").argv[1],\"wb\").write(b\"X\")' {candidate_path}",
            },
            "expected_effect": {"description": "reach parser"},
            "previous_feedback": {"stage": "H1", "response": "will address H1"},
        },
        workspace=tmp_path,
        reasoning_state_path=state,
    )
    assert rejected["accepted"] is False
    assert any("positive output evidence" in error for error in rejected["errors"])

    accepted = record_candidate_plan(
        plan={
            "hypothesis": {"summary": "deep parser bug"},
            "target_input_component": {"description": "external scratch builder"},
            "construction_strategy": {"mode": "external_builder"},
            "builder": {
                "kind": "external_command",
                "command": "python3 -c 'open(__import__(\"sys\").argv[1],\"wb\").write(bytes.fromhex(\"544f59\"))' {candidate_path}",
            },
            "output_contract": {"required_prefix_hex": "544f59", "min_size": 3},
            "expected_effect": {"description": "reach parser"},
            "previous_feedback": {"stage": "H1", "response": "will address H1 with checked artifact"},
        },
        workspace=tmp_path,
        reasoning_state_path=state,
    )
    assert accepted["accepted"] is True


def test_build_candidate_validates_output_contract(tmp_path: Path) -> None:
    state = tmp_path / "reasoning_state.json"
    state.write_text(
        json.dumps(
            {
                "primary_source": {"file": "parser.c", "function": "parse", "line": 10},
                "primary_root_cause": {"file": "parser.c", "function": "parse", "line": 20},
                "primary_sink": {"file": "parser.c", "function": "parse", "line": 30},
                "trace": [{"step": 1, "from": "input", "to": "sink", "relation": "parse"}],
                "next_missing": [],
                "snapshot": {"event_id": 1, "note": "toy parser bug"},
            }
        ),
        encoding="utf-8",
    )
    plan = record_candidate_plan(
        plan={
            "hypothesis": {"summary": "toy parser"},
            "target_input_component": {"description": "toy container"},
            "construction_strategy": {"mode": "external_builder"},
            "builder": {
                "kind": "external_command",
                "command": "python3 -c 'open(__import__(\"sys\").argv[1],\"wb\").write(b\"NO\")' {candidate_path}",
            },
            "output_contract": {
                "required_prefix_hex": "544f59",
                "validation_command": "python3 -c 'import pathlib,sys; data=pathlib.Path(sys.argv[1]).read_bytes(); sys.exit(0 if data.startswith(b\"TOY\") else 7)' {candidate_path}",
            },
            "expected_effect": {"description": "reach parser"},
        },
        workspace=tmp_path,
        reasoning_state_path=state,
    )
    assert plan["accepted"] is True

    candidate = build_candidate(plan_id=plan["plan_id"], workspace=tmp_path)

    assert candidate["built"] is False
    assert any("required_prefix_hex" in error for error in candidate["errors"])
    assert any("validation_command failed" in error for error in candidate["errors"])


def test_seed_mutation_requires_real_seed(tmp_path: Path) -> None:
    state = tmp_path / "reasoning_state.json"
    state.write_text(
        json.dumps(
            {
                "primary_source": {"file": "parser.c", "function": "parse", "line": 10},
                "primary_root_cause": {"file": "parser.c", "function": "parse", "line": 20},
                "primary_sink": {"file": "parser.c", "function": "parse", "line": 30},
                "trace": [
                    {
                        "step": 1,
                        "from": "input",
                        "to": "sink",
                        "relation": "parse",
                        "file": "parser.c",
                        "function": "parse",
                        "line": 10,
                    }
                ],
                "next_missing": [],
                "snapshot": {"event_id": 1, "note": "deep parser bug"},
            }
        ),
        encoding="utf-8",
    )

    rejected = record_candidate_plan(
        plan={
            "hypothesis": {"summary": "deep parser bug"},
            "target_input_component": {"description": "claim seed mutation"},
            "construction_strategy": {"mode": "seed_mutation"},
            "builder": {
                "kind": "external_command",
                "command": "python3 -c 'open(__import__(\"sys\").argv[1],\"wb\").write(b\"X\")' {candidate_path}",
            },
            "expected_effect": {"description": "reach parser"},
        },
        workspace=tmp_path,
        reasoning_state_path=state,
    )

    assert rejected["accepted"] is False
    assert any("seed_mutation requires a real seed" in error for error in rejected["errors"])
