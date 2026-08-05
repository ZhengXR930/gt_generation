from experiments.runtime_hypothesis_feedback.trace_mapper import (
    apply_mapping,
    apply_probe_plan,
    compile_runtime_observation_specs,
    derive_key_branch_checkpoints,
    derive_read_raw_data_checkpoints,
    validate_mapping,
    validate_probe_plan,
    validate_trace_source_anchors,
)


TRACE = [
    {"step": 1, "function": "harness", "var": "data", "code": "parse(data)", "note": "input"},
    {"step": 2, "function": "parse", "var": "len", "code": "read_len()", "note": "header accepted"},
    {"step": 3, "function": "copy", "var": "len", "code": "memcpy(dst, src, len)", "note": "unchecked copy"},
    {"step": 4, "function": "use", "var": "dst", "code": "return dst[0]", "note": "downstream read"},
]


def test_mapping_is_grounded_and_ordered():
    mapping = validate_mapping(
        {
            "admission_step": 2, "admission_evidence": "header accepted",
            "root_step": 3, "root_evidence": "unchecked copy",
            "consumer_step": 4, "consumer_evidence": "downstream read",
            "runtime_observations": [
                {"step": 3, "name": "copy_length", "expression": "len"}
            ],
            "reason": "anchors are present",
        },
        TRACE,
    )
    enriched = apply_mapping(TRACE, mapping, "length must fit destination")
    assert enriched[1]["phase"] == "admission"
    assert enriched[2]["role"] == "root"
    assert enriched[3]["role"] == "sink"
    assert enriched[2]["captures"] == {"observer_copy_length": "len"}
    assert enriched[2]["observer_capture_names"] == ["observer_copy_length"]
    assert "role" not in TRACE[2]


def test_dereference_observation_also_captures_pointer_address():
    pointer_trace = [
        {"step": 1, "function": "next", "var": "*lexer->pos", "note": "read byte"}
    ]
    mapping = validate_mapping(
        {
            "admission_step": None, "admission_evidence": None,
            "root_step": 1, "root_evidence": "*lexer->pos",
            "consumer_step": None, "consumer_evidence": None,
            "runtime_observations": [
                {"step": 1, "name": "byte", "expression": "*lexer->pos"}
            ],
            "reason": "observe value and address",
        },
        pointer_trace,
    )
    enriched = apply_mapping(pointer_trace, mapping, "read stays in bounds")
    assert enriched[0]["captures"] == {
        "observer_byte": "*lexer->pos",
        "observer_byte_address": "lexer->pos",
    }
    assert enriched[0]["observer_capture_kinds"] == {
        "observer_byte": "dereferenced_value",
        "observer_byte_address": "address",
    }


def test_mapping_rejects_invented_evidence():
    try:
        validate_mapping(
            {
                "admission_step": None, "admission_evidence": None,
                "root_step": 3, "root_evidence": "not in trace",
                "consumer_step": None, "consumer_evidence": None,
                "reason": "bad",
            },
            TRACE,
        )
    except ValueError as exc:
        assert "evidence" in str(exc)
    else:
        raise AssertionError("invented evidence accepted")


def test_admission_evidence_is_semantically_selected_not_keyword_filtered():
    mapping = validate_mapping(
        {
            "admission_step": 2, "admission_evidence": "read_len()",
            "root_step": 3, "root_evidence": "unchecked copy",
            "consumer_step": None, "consumer_evidence": None,
            "reason": "the semantic mapper selected the admission step",
        },
        TRACE,
    )
    assert mapping["admission_step"] == 2
    assert mapping["root_step"] == 3


def test_out_of_order_consumer_is_dropped_without_losing_root():
    mapping = validate_mapping(
        {
            "admission_step": 2, "admission_evidence": "header accepted",
            "root_step": 3, "root_evidence": "unchecked copy",
            "consumer_step": 1, "consumer_evidence": "input",
            "reason": "consumer selection was noisy",
        },
        TRACE,
    )
    assert mapping["root_step"] == 3
    assert mapping["consumer_step"] is None
    assert mapping["consumer_evidence"] is None


def test_runtime_observation_must_be_passive_and_trace_grounded():
    base = {
        "admission_step": 2, "admission_evidence": "header accepted",
        "root_step": 3, "root_evidence": "unchecked copy",
        "consumer_step": None, "consumer_evidence": None,
        "reason": "observe the copy length",
    }
    for expression in ("guess", "memcpy(dst, src, len)", "len = 999"):
        mapping = validate_mapping(
            {
                **base,
                "runtime_observations": [
                    {"step": 3, "name": "value", "expression": expression}
                ],
            },
            TRACE,
        )
        assert mapping["runtime_observations"] == []
        assert len(mapping["runtime_observation_rejections"]) == 1


def test_legacy_mapping_without_observations_defaults_to_empty_list():
    mapping = validate_mapping(
        {
            "admission_step": 2, "admission_evidence": "header accepted",
            "root_step": 3, "root_evidence": "unchecked copy",
            "consumer_step": None, "consumer_evidence": None,
            "reason": "legacy cached mapping",
        },
        TRACE,
    )
    assert mapping["runtime_observations"] == []
    assert mapping["runtime_observation_rejections"] == []


def test_assignment_statement_is_reduced_to_passive_rhs_capture():
    mapping = validate_mapping(
        {
            "admission_step": 2, "admission_evidence": "header accepted",
            "root_step": 3, "root_evidence": "unchecked copy",
            "consumer_step": None, "consumer_evidence": None,
            "runtime_observations": [
                {"step": 3, "name": "copy_length", "expression": "memcpy(dst, src, len)"},
                {"step": 2, "name": "parsed_length", "expression": "value = len"},
            ],
            "reason": "observe passive values",
        },
        TRACE,
    )
    # The call remains rejected. The assignment is grounded because `len` is
    # present in the selected step and is reduced to a passive expression.
    assert mapping["runtime_observations"] == [
        {"step": 2, "name": "parsed_length", "expression": "len"}
    ]
    assert len(mapping["runtime_observation_rejections"]) == 1


def test_minimal_probe_plan_is_grounded_and_compiles_for_gdb():
    plan = validate_probe_plan(
        {
            "probes": [
                {"stage": "admission", "step": 2, "captures": []},
                {
                    "stage": "root",
                    "step": 3,
                    "captures": [{"name": "length", "expression": "len"}],
                },
                {"stage": "propagation", "step": 4, "captures": []},
            ]
        },
        TRACE,
    )
    enriched = apply_probe_plan(TRACE, plan, "length must fit destination")
    assert enriched[1]["phase"] == "admission"
    assert enriched[2]["role"] == "root"
    assert enriched[2]["captures"] == {"observer_root_length": "len"}
    assert enriched[3]["role"] == "sink"


def test_minimal_probe_plan_rejects_invented_capture():
    plan = validate_probe_plan(
        {
            "probes": [
                {
                    "stage": "root",
                    "step": 3,
                    "captures": [{"name": "capacity", "expression": "capacity"}],
                }
            ]
        },
        TRACE,
    )
    assert plan == {
        "probes": [{"stage": "root", "step": 3, "captures": []}],
        "call_observations": [],
        "branch_observations": [],
    }


def test_unsafe_capture_does_not_discard_other_captures_or_probe():
    plan = validate_probe_plan(
        {
            "probes": [
                {
                    "stage": "root",
                    "step": 3,
                    "captures": [
                        {"name": "unsafe", "expression": "memcpy(dst, src, len)"},
                        {"name": "length", "expression": "len"},
                    ],
                }
            ]
        },
        TRACE,
    )
    assert plan["probes"][0]["captures"] == [
        {"name": "length", "expression": "len"}
    ]


def test_noisy_probe_plan_is_bounded_without_discarding_valid_stages():
    plan = validate_probe_plan(
        {
            "probes": [
                {"stage": "admission", "step": 1, "captures": []},
                {"stage": "admission", "step": 2, "captures": []},
                {"stage": "admission", "step": 3, "captures": []},
                {"stage": "root", "step": 3, "captures": []},
                {"stage": "root", "step": 3, "captures": []},
                {"stage": "root", "step": 1, "captures": []},
                {"stage": "propagation", "step": 4, "captures": []},
                {"stage": "propagation", "step": 3, "captures": []},
                {"stage": "propagation", "step": 2, "captures": []},
            ],
            "call_observations": [
                {"stage": "root", "step": 3},
                {"stage": "root", "step": 3},
                {"stage": "propagation", "step": 4},
                {"stage": "admission", "step": 2},
                {"stage": "propagation", "step": 3},
            ],
            "branch_observations": [],
        },
        TRACE,
    )
    assert [(probe["stage"], probe["step"]) for probe in plan["probes"]] == [
        ("admission", 1),
        ("admission", 2),
        ("root", 3),
        ("root", 1),
        ("propagation", 4),
        ("propagation", 3),
    ]
    assert len(plan["call_observations"]) == 4


def test_trace_line_code_mismatch_is_repaired_within_function(tmp_path):
    source = tmp_path / "src-vul" / "decoder.cpp"
    source.parent.mkdir()
    source.write_text(
        "static bool Load(int n)\n"
        "{\n"
        "    stream.readRawData(dst, n);\n"
        "    return true;\n"
        "}\n",
        encoding="utf-8",
    )
    trace = [{
        "step": 1,
        "file": "src-vul/decoder.cpp",
        "function": "Load",
        "line": 2,
        "code": "stream.readRawData(dst, n);",
        "note": "claimed callsite",
    }]
    validated = validate_trace_source_anchors(trace, tmp_path)
    anchor = validated[0]["anchor_validation"]
    assert anchor["status"] == "repaired"
    assert anchor["reason"] == "line_code_relocated_within_function"
    assert anchor["source_text"] == "{"
    assert anchor["matching_lines"] == [3]
    assert anchor["declared_line"] == 2
    assert anchor["resolved_line"] == 3
    assert validated[0]["line"] == 3


def test_trace_exact_source_statement_is_valid(tmp_path):
    source = tmp_path / "decoder.cpp"
    source.write_text("void Load() {\n  consume(value);\n}\n", encoding="utf-8")
    validated = validate_trace_source_anchors([{
        "step": 1, "file": "decoder.cpp", "function": "Load", "line": 2,
        "code": "consume(value);", "note": "consume",
    }], tmp_path)
    assert validated[0]["anchor_validation"]["status"] == "valid"


def test_trace_multistatement_claim_expands_from_declared_first_line(tmp_path):
    source = tmp_path / "decoder.cpp"
    source.write_text(
        "void Load() {\n"
        "  count *= pixel_size;\n"
        "  stream.readRawData(dst, count);\n"
        "}\n",
        encoding="utf-8",
    )
    validated = validate_trace_source_anchors([{
        "step": 1, "file": "decoder.cpp", "function": "Load", "line": 2,
        "code": "count *= pixel_size; stream.readRawData(dst, count);",
        "note": "the short read leaves the destination incomplete",
    }], tmp_path)
    anchor = validated[0]["anchor_validation"]
    assert anchor["status"] == "repaired"
    assert anchor["resolved_line"] == 2
    assert anchor["resolved_line_end"] == 3
    assert validated[0]["line_end"] == 3


def test_read_raw_data_probes_are_derived_from_public_source(tmp_path):
    source = tmp_path / "src-vul" / "decoder.cpp"
    source.parent.mkdir()
    source.write_text(
        "static bool Load(Stream &stream, Info info) {\n"
        "  uchar c;\n"
        "  unsigned count = 3;\n"
        "  stream.readRawData(dst, count);\n"
        "  return true;\n"
        "}\n",
        encoding="utf-8",
    )
    reward_spec = {"stages": {"root": {"where": [{
        "file": "src-vul/decoder.cpp", "function": "Load"
    }]}}}
    probes = derive_read_raw_data_checkpoints(
        codebase=tmp_path, reward_spec=reward_spec, trace=[]
    )
    assert len(probes) == 1
    probe = probes[0]
    assert probe["file"] == "decoder.cpp"
    assert probe["line"] == 4
    assert probe["captures"] == {"requested_bytes": "(int)$edx"}
    assert probe["caller_captures"] == {}
    assert probe["source_requested_expression"] == "count"
    assert probe["static_branch_facts"] == {"packet_is_rle": False}
    assert probe["break_function"] == "QDataStream::readRawData"
    assert probe["return_capture"] == "returned_bytes"


def test_key_branch_probes_record_source_control_facts(tmp_path):
    source = tmp_path / "src-vul" / "decoder.cpp"
    source.parent.mkdir()
    source.write_text(
        "static bool Load(Info info) {\n"
        "  if (info.pal) {\n"
        "    uchar idx = *src++;\n"
        "  } else if (info.grey) {\n"
        "    out = qRgb(*src, *src, *src);\n"
        "  } else {\n"
        "    if (tga.pixel_size == 24) out = qRgb(src[2], src[1], src[0]);\n"
        "  }\n"
        "  return true;\n"
        "}\n",
        encoding="utf-8",
    )
    reward_spec = {"stages": {"root": {"where": [{
        "file": "src-vul/decoder.cpp", "function": "Load"
    }]}}}
    probes = derive_key_branch_checkpoints(
        codebase=tmp_path, reward_spec=reward_spec, trace=[]
    )
    assert [probe["line"] for probe in probes] == [3, 5, 7]
    assert [probe["static_branch_facts"] for probe in probes] == [
        {"palette_mode": True, "grey_mode": False},
        {"palette_mode": False, "grey_mode": True},
        {"palette_mode": False, "grey_mode": False},
    ]
    assert all(not probe["allow_function_fallback"] for probe in probes)


def test_generic_call_and_branch_specs_compile_without_api_names(tmp_path):
    source = tmp_path / "src-vul" / "decoder.cpp"
    source.parent.mkdir()
    source.write_text(
        "static bool Load(Stream &stream, unsigned pixel_size) {\n"
        "  unsigned char c = next();\n"
        "  if (c & 0x80) {\n"
        "    char pixel[8];\n"
        "    stream.readRawData(pixel, pixel_size);\n"
        "  } else {\n"
        "    char *dst = buffer;\n"
        "    stream.readRawData(dst, pixel_size);\n"
        "  }\n"
        "  return true;\n"
        "}\n",
        encoding="utf-8",
    )
    trace = validate_trace_source_anchors([
        {
            "step": 1, "file": "src-vul/decoder.cpp", "function": "Load",
            "line": 3, "var": "c", "code": "if (c & 0x80) {",
            "note": "the c & 0x80 packet branch controls decoding",
        },
        {
            "step": 2, "file": "src-vul/decoder.cpp", "function": "Load",
            "line": 5, "var": "pixel_size",
            "code": "stream.readRawData(pixel, pixel_size);",
            "note": "compare returned bytes with pixel_size",
        },
    ], tmp_path)
    plan = validate_probe_plan({
        "probes": [{"stage": "root", "step": 2, "captures": []}],
        "call_observations": [],
        "branch_observations": [{
            "stage": "admission", "step": 1, "predicate": "c & 0x80",
        }],
    }, trace)
    checkpoints = compile_runtime_observation_specs(
        codebase=tmp_path, trace=trace, plan=plan
    )
    call = next(item for item in checkpoints if item["kind"] == "call_observation")
    assert call["break_function"] == "Stream::readRawData"
    assert call["captures"] == {
        "pixel": "(long long)$rsi", "pixel_size": "(long long)$rdx",
    }
    assert call["argument_metadata"] == [
        {"index": 0, "name": "pixel", "source_expression": "pixel"},
        {"index": 1, "name": "pixel_size", "source_expression": "pixel_size"},
    ]
    assert call["return_capture"] == "return_value"
    assert call["derived_relations"] == [{
        "name": "return_value_lt_pixel_size", "op": "lt",
        "left": "return_value", "right": "pixel_size",
    }]
    branches = [item for item in checkpoints if item["kind"] == "branch_observation"]
    assert [(item["line"], item["branch_outcome"]) for item in branches] == [
        (5, True), (7, False),
    ]
    assert all(item["branch_predicate"] == "c & 0x80" for item in branches)
