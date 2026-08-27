import json
import sqlite3
import sys
from pathlib import Path

from docker.errors import DockerException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "external" / "cybergym" / "src"))

from cybergym.server.__main__ import validate_candidate_trace
from cybergym.server.pocdb import create_submission_attempt, init_engine
from harness_runtime.dedup import (
    deduplicate_submission_attempts,
)
from harness_runtime.openhands.arvo import runtime_server_url
from harness_runtime.workspace import validate_candidate_input_bytes
from sqlalchemy.orm import Session


VALID_TRACE = [
    {
        "step": 1,
        "file": "src/parser.c",
        "function": "parse",
        "line": 10,
        "var": "input",
        "code": "parse(input)",
        "note": "Attacker-controlled bytes enter the parser.",
    }
]


def test_candidate_trace_validator_accepts_gt_shape():
    content = json.dumps(VALID_TRACE).encode()
    assert validate_candidate_trace(content) is None


def test_candidate_trace_validator_rejects_bad_step_sequence():
    trace = [dict(VALID_TRACE[0], step=2)]
    assert "step=1" in validate_candidate_trace(json.dumps(trace).encode())


def test_candidate_trace_validator_rejects_explicit_dependency_edges():
    trace = [dict(VALID_TRACE[0], depends_on=[])]
    error = validate_candidate_trace(json.dumps(trace).encode())

    assert "must not contain depends_on" in error


def test_candidate_input_guard_rejects_analysis_artifacts():
    payload = {
        "sample_id": "arvo_1",
        "fine_trace": VALID_TRACE,
        "vuln_logic": {"source": {}, "root_cause": {}, "sink": {}, "propagation": []},
    }

    error = validate_candidate_input_bytes(json.dumps(payload).encode())

    assert "analysis artifact" in error


def test_candidate_input_guard_rejects_prompt_text():
    content = b"""# PoC generation task: arvo_1
Workspace layout:
Use /workspace/submit.sh after writing analysis.json artifact.
"""

    error = validate_candidate_input_bytes(content)

    assert "prompt/report text" in error


def test_candidate_input_guard_allows_raw_binary_input():
    assert validate_candidate_input_bytes(b"\xff\x00candidate") is None


def test_submission_attempts_do_not_deduplicate(tmp_path):
    db_path = tmp_path / "poc.db"
    engine = init_engine(db_path)
    with Session(engine) as session:
        for attempt_id in ("attempt-a", "attempt-b"):
            create_submission_attempt(
                session,
                attempt_id=attempt_id,
                agent_id="agent",
                task_id="arvo:1",
                poc_hash="same-hash",
                poc_length=4,
                trace_valid=True,
                trace_error=None,
            )
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT count(*) FROM submission_attempts"
        ).fetchone()[0]
    assert count == 2


def test_evaluation_deduplication_keeps_last_trace_and_reports_ratios():
    attempts = [
        {
            "attempt_id": "first-a",
            "poc_hash": "hash-a",
            "result_path": "submissions/first-a/",
            "sequence_in_run": 1,
            "vul_exit_code": 0,
        },
        {
            "attempt_id": "only-b",
            "poc_hash": "hash-b",
            "result_path": "submissions/only-b/",
            "sequence_in_run": 2,
            "vul_exit_code": 1,
        },
        {
            "attempt_id": "last-a",
            "poc_hash": "hash-a",
            "result_path": "submissions/last-a/",
            "sequence_in_run": 3,
            "vul_exit_code": 0,
        },
    ]

    stats, representatives = deduplicate_submission_attempts(attempts)

    assert stats["total_poc_submissions"] == 3
    assert stats["deduplicated_poc_count"] == 2
    assert stats["duplicate_poc_submissions"] == 1
    assert stats["deduplicated_ratio"] == 0.666667
    assert stats["duplicate_ratio"] == 0.333333
    by_hash = {item["poc_sha256"]: item for item in representatives}
    assert by_hash["hash-a"]["representative_attempt_id"] == "last-a"
    assert (
        by_hash["hash-a"]["representative_analysis_path"]
        == "submissions/last-a/analysis.json"
    )


def test_prompt_binds_each_poc_to_analysis_artifact():
    prompt = (ROOT / "poc_generation" / "prompt.txt").read_text(encoding="utf-8")
    assert "Read\n/workspace/description.txt first" in prompt
    assert "bash /workspace/submit.sh /path/to/candidate /workspace/analysis.json" in prompt
    assert "/workspace/README.md" not in prompt
    assert "A task may finish without a submission" not in prompt
    assert "R1" not in prompt


def test_linux_runtime_server_falls_back_to_default_docker_bridge(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("OPENHANDS_EVAL_HOST_GATEWAY", raising=False)
    class DockerModule:
        @staticmethod
        def from_env():
            raise DockerException("unavailable")

    monkeypatch.setattr(
        "harness_runtime.openhands.arvo._docker_sdk",
        lambda: (DockerModule, object),
    )
    assert (
        runtime_server_url("http://host.docker.internal:8666")
        == "http://172.17.0.1:8666"
    )
