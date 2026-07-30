import json

from evaluator.reasoning.fine_trace import (
    parse_fine_trace,
    validate_fine_trace,
    write_fine_trace,
)


def _trace():
    return [
        {
            "step": 1,
            "file": "parser.c",
            "function": "parse",
            "line": 12,
            "var": "input",
            "code": "value = read(input);",
            "note": "Attacker-controlled value enters the parser.",
        },
        {
            "step": 2,
            "file": "parser.c",
            "function": "parse",
            "line": 27,
            "var": "value",
            "code": "buf[value] = 0;",
            "note": "Unchecked value reaches the out-of-bounds write.",
        },
    ]


def test_subject_fine_trace_uses_gt_shape_and_persists_bare_array(tmp_path):
    response = json.dumps(_trace())

    assert validate_fine_trace(response) is None
    assert parse_fine_trace(response) == _trace()

    output = tmp_path / "fine_trace.json"
    write_fine_trace(output, response)
    assert json.loads(output.read_text()) == _trace()


def test_subject_fine_trace_rejects_prose_fences_and_nonsequential_steps():
    response = "```json\n" + json.dumps(_trace()) + "\n```"
    assert "bare JSON array" in validate_fine_trace(response)

    trace = _trace()
    trace[1]["step"] = 3
    assert "step=2" in validate_fine_trace(json.dumps(trace))


def test_subject_fine_trace_rejects_explicit_dependency_edges():
    trace = _trace()
    trace[1]["depends_on"] = [{"on": 1, "type": "data", "via": "value"}]

    assert "must not contain depends_on" in validate_fine_trace(json.dumps(trace))
