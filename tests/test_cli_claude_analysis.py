import json

from harness_runtime import cli


def _analysis(sample_id: str = "arvo_1") -> dict:
    return {
        "sample_id": sample_id,
        "fine_trace": [
            {
                "step": 1,
                "file": "src/foo.c",
                "function": "parse",
                "line": 10,
                "var": "len",
                "code": "len = input_len;",
                "note": "Attacker-controlled length enters the parser.",
                "role": "source",
            },
            {
                "step": 2,
                "file": "src/foo.c",
                "function": "parse",
                "line": 20,
                "var": "len",
                "code": "if (len > buf_size) oversized = len;",
                "note": "The violated bound is identified before the copy.",
                "role": "root_cause",
            },
            {
                "step": 3,
                "file": "src/foo.c",
                "function": "parse",
                "line": 30,
                "var": "buf[len]",
                "code": "memcpy(buf, input, len);",
                "note": "The unchecked length reaches the copy sink.",
                "role": "sink",
            },
        ],
        "vuln_logic": {
            "source": {
                "file": "src/foo.c",
                "function": "parse",
                "line": 10,
                "operands": ["len"],
            },
            "root_cause": {
                "file": "src/foo.c",
                "function": "parse",
                "line": 20,
                "operands": ["len", "buf_size"],
                "relation": {"op": "gt", "left": "len", "right": "buf_size"},
            },
            "sink": {
                "file": "src/foo.c",
                "function": "parse",
                "line": 30,
                "operands": ["buf[len]", "len"],
                "relation": {"op": "gt", "left": "len", "right": "buf_size"},
            },
            "propagation": [
                {
                    "from": {
                        "file": "src/foo.c",
                        "function": "parse",
                        "line": 10,
                        "operands": ["len"],
                    },
                    "to": {
                        "file": "src/foo.c",
                        "function": "parse",
                        "line": 20,
                        "operands": ["len"],
                    },
                    "type": "data",
                    "via": ["len"],
                },
                {
                    "from": {
                        "file": "src/foo.c",
                        "function": "parse",
                        "line": 20,
                        "operands": ["len"],
                    },
                    "to": {
                        "file": "src/foo.c",
                        "function": "parse",
                        "line": 30,
                        "operands": ["len"],
                    },
                    "type": "data",
                    "via": ["len"],
                },
            ],
        },
    }


def test_claude_final_message_fenced_analysis_is_persisted_as_candidate(tmp_path):
    workspace = tmp_path / "workspace"
    sample_dir = tmp_path / "results" / "arvo_1"
    workspace.mkdir()
    sample_dir.mkdir(parents=True)
    (workspace / "analysis.json").write_text(
        json.dumps(
            {
                "sample_id": "arvo_1",
                "fine_trace": [],
                "vuln_logic": {"summary": "not valid"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    final_message = "Here is the artifact:\n```json\n" + json.dumps(_analysis()) + "\n```"

    extraction = cli._persist_final_analysis_from_text(final_message, workspace, "arvo_1")
    produced, source, diagnostics = cli._copy_latest_analysis(workspace, sample_dir)

    assert extraction == {
        "extracted": True,
        "path": ".final_analysis.json",
        "source": "fenced_json_1",
    }
    assert produced is True
    assert source == ".final_analysis.json"
    assert json.loads((sample_dir / "analysis.json").read_text())["sample_id"] == "arvo_1"
    assert diagnostics[0]["path"] == "analysis.json"
    assert diagnostics[0]["accepted"] is False
    assert diagnostics[1] == {"path": ".final_analysis.json", "accepted": True}


def test_claude_final_message_does_not_overwrite_workspace_analysis(tmp_path):
    workspace = tmp_path / "workspace"
    sample_dir = tmp_path / "results" / "arvo_1"
    workspace.mkdir()
    sample_dir.mkdir(parents=True)
    workspace_analysis = _analysis()
    (workspace / "analysis.json").write_text(
        json.dumps(workspace_analysis, indent=2) + "\n",
        encoding="utf-8",
    )
    other_message = json.dumps(_analysis("arvo_2"))

    extraction = cli._persist_final_analysis_from_text(other_message, workspace, "arvo_1")
    produced, source, diagnostics = cli._copy_latest_analysis(workspace, sample_dir)

    assert extraction["extracted"] is True
    assert extraction["warning"].startswith("sample_id_mismatch")
    assert produced is True
    assert source == "analysis.json"
    assert json.loads((workspace / "analysis.json").read_text()) == workspace_analysis
    assert diagnostics == [{"path": "analysis.json", "accepted": True}]
