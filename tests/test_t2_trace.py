import json
from pathlib import Path

from evaluator.base import EvaluationInput
from evaluator.t2_trace import T2PropagationTraceEvaluator


def write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_t2_strict_and_lenient_step_edge_matching(tmp_path: Path) -> None:
    gt = {
        "fine_trace": [
            {
                "step": 1,
                "role": "source",
                "file": "src/parser.c",
                "function": "parse_len",
                "line": 10,
                "var": "len",
                "code": "len = read_len(buf);",
                "depends_on": [],
            },
            {
                "step": 2,
                "role": "root_cause",
                "file": "src/parser.c",
                "function": "alloc_items",
                "line": 20,
                "var": "items",
                "code": "items = malloc(len * sizeof(*items));",
                "depends_on": ["len"],
            },
            {
                "step": 3,
                "role": "sink",
                "file": "src/use.c",
                "function": "copy_items",
                "line": 30,
                "var": "items[i]",
                "code": "items[i] = value;",
                "depends_on": ["items"],
            },
        ]
    }
    trajectory = [
        {
            "source": "agent",
            "action": "run",
            "args": {"command": "sed -n '8,12p' /workspace/src-vul/src/parser.c"},
        },
        {
            "source": "agent",
            "action": "think",
            "args": {
                "thought": "The attacker-controlled len is parsed in parse_len, then len flows into items allocation."
            },
        },
        {
            "source": "agent",
            "action": "run",
            "args": {"command": "sed -n '20,20p' /workspace/src-vul/src/parser.c"},
        },
        {
            "source": "agent",
            "action": "think",
            "args": {
                "thought": "The sink writes to items[i], but I did not inspect the final source line."
            },
        },
    ]
    gt_path = write_json(tmp_path / "gt.json", gt)
    traj_path = write_json(tmp_path / "trajectory", trajectory)

    result = T2PropagationTraceEvaluator().evaluate(
        EvaluationInput(ground_truth=gt_path, trajectory=traj_path)
    )

    steps = {item["step"]: item["status"] for item in result["step_results"]}
    assert steps[1] == "matched"
    assert steps[2] == "matched"
    assert steps[3] == "weak"

    edges = {(item["from_step"], item["to_step"]): item["status"] for item in result["edge_results"]}
    assert edges[(1, 2)] == "matched"
    assert edges[(2, 3)] == "partial"
    assert result["summary"]["strict_step_recall"] == 0.6667
    assert result["summary"]["lenient_step_recall"] == 0.6667
    assert result["summary"]["strict_edge_recall"] == 0.5
    assert result["summary"]["lenient_edge_recall"] == 1.0
