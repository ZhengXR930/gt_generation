from experiments.runtime_hypothesis_feedback.semantic_supervisor import (
    NEUTRAL_COMMITMENT,
    SemanticCandidateSupervisor,
    validate_gate_decision,
    validate_readiness,
)


def run_event(command: str) -> dict:
    return {
        "source": "agent",
        "action": "run",
        "args": {"command": command},
    }


def decision(kind: str) -> dict:
    return {
        "proposed_action_role": "broad_analysis" if kind == "redirect" else "resolves_execution_blocker",
        "concrete_blocker": None,
        "reason": "the proposed read broadens analysis" if kind == "redirect" else "it resolves the input-layout blocker",
    }


def readiness(ready: bool) -> dict:
    return {
        "ready": ready,
        "concrete_blocker": None if ready else "input representation is unknown",
        "commitment": "write a minimally parseable frame" if ready else None,
        "interface_evidence": "harness reads raw bytes" if ready else None,
        "representation_evidence": "frame starts with a header" if ready else None,
        "reason": "a runnable frame can now be serialized" if ready else "the frame layout has not been inspected",
    }


OBSERVED_HISTORY = """--BEGIN AGENT OBSERVATION--
harness reads raw bytes; frame starts with a header
--END AGENT OBSERVATION--"""


def test_semantic_redirect_has_no_counter_threshold(tmp_path):
    injected = []
    calls = []

    def judge(*args, **kwargs):
        calls.append(args[2])
        return decision("redirect")

    supervisor = SemanticCandidateSupervisor(
        skeleton={},
        log_path=tmp_path / "semantic.jsonl",
        api_key="test",
        inject_message=injected.append,
        judge=judge,
        readiness_judge=lambda *args, **kwargs: readiness(True),
    )
    assert not supervisor.before_action(
        run_event("grep -R parser repo-vul"),
        "grep -R parser repo-vul",
        OBSERVED_HISTORY,
    )
    assert len(calls) == 1
    assert len(injected) == 1
    assert NEUTRAL_COMMITMENT in injected[0]
    assert "minimally parseable frame" not in injected[0]


def test_concrete_blocker_action_is_allowed(tmp_path):
    supervisor = SemanticCandidateSupervisor(
        skeleton={},
        log_path=tmp_path / "semantic.jsonl",
        api_key="test",
        inject_message=lambda message: None,
        judge=lambda *args, **kwargs: decision("allow"),
        readiness_judge=lambda *args, **kwargs: readiness(True),
    )
    assert supervisor.before_action(
        run_event("sed -n '1,80p' harness.cc"),
        "read the harness to resolve the required invocation arguments",
        OBSERVED_HISTORY,
    )


def test_submission_bypasses_llm_but_revision_remains_gated(tmp_path):
    calls = []
    injected = []
    supervisor = SemanticCandidateSupervisor(
        skeleton={},
        log_path=tmp_path / "semantic.jsonl",
        api_key="test",
        inject_message=injected.append,
        judge=lambda *args, **kwargs: (calls.append(1), decision("redirect"))[1],
    )
    assert supervisor.before_action(
        run_event("bash ./submit.sh /workspace/poc /workspace/candidate_trace.json"),
        "submit candidate",
        "history",
    )
    assert supervisor.has_submitted
    supervisor.commit_required = True
    supervisor.commitment = NEUTRAL_COMMITMENT
    assert not supervisor.before_action(
        run_event("grep unrelated source.c"), "broad analysis after reward", "history"
    )
    assert calls == [1]
    assert injected


def test_finish_redirect_does_not_block_trace_finalization(tmp_path):
    injected = []
    supervisor = SemanticCandidateSupervisor(
        skeleton={},
        log_path=tmp_path / "semantic.jsonl",
        api_key="test",
        inject_message=injected.append,
    )
    assert not supervisor.before_finish(fine_trace_finalization=False)
    assert supervisor.before_finish(fine_trace_finalization=True)
    assert len(injected) == 1


def test_verified_target_success_is_terminal(tmp_path):
    calls = []
    supervisor = SemanticCandidateSupervisor(
        skeleton={},
        log_path=tmp_path / "semantic.jsonl",
        api_key="test",
        inject_message=lambda message: None,
        judge=lambda *args, **kwargs: calls.append(1),
    )
    history = 'hypothesis_feedback: {"target":{"triggered":true}}'
    assert supervisor.before_action(run_event("inspect after success"), "inspect", history)
    assert supervisor.before_finish(fine_trace_finalization=False, raw_history=history)
    assert calls == []


def test_non_crashing_submission_cannot_finish_early(tmp_path):
    injected = []
    supervisor = SemanticCandidateSupervisor(
        skeleton={},
        log_path=tmp_path / "semantic.jsonl",
        api_key="test",
        inject_message=injected.append,
        has_submitted=True,
    )
    history = 'hypothesis_feedback: {"target":{"triggered":false}}'
    assert not supervisor.before_finish(fine_trace_finalization=False, raw_history=history)
    assert injected


def test_semantic_facts_compute_redirect_deterministically():
    value = validate_gate_decision(decision("redirect"))
    assert value["proposed_action_role"] == "broad_analysis"


def test_no_concrete_blocker_text_is_normalized():
    value = decision("allow")
    value["concrete_blocker"] = "No concrete blocker identified; still exploring"
    assert validate_gate_decision(value)["concrete_blocker"] is None


def test_not_ready_history_does_not_gate_proposed_action(tmp_path):
    classifier_calls = []
    supervisor = SemanticCandidateSupervisor(
        skeleton={},
        log_path=tmp_path / "semantic.jsonl",
        api_key="test",
        inject_message=lambda message: None,
        judge=lambda *args, **kwargs: classifier_calls.append(1),
        readiness_judge=lambda *args, **kwargs: readiness(False),
    )
    assert supervisor.before_action(
        run_event("grep more source.c"), "broaden source exploration", "history"
    )
    assert classifier_calls == []


def test_readiness_requires_commitment():
    value = readiness(True)
    value["commitment"] = None
    try:
        validate_readiness(value, "harness reads raw bytes frame starts with a header")
    except ValueError as exc:
        assert "commitment" in str(exc)
    else:
        raise AssertionError("ready decision without commitment was accepted")


def test_readiness_evidence_must_come_from_observation():
    try:
        validate_readiness(readiness(True), "unrelated observation")
    except ValueError as exc:
        assert "grounded" in str(exc)
    else:
        raise AssertionError("ungrounded readiness was accepted")


def test_readiness_accepts_grounded_brief_paraphrase():
    value = readiness(True)
    value["interface_evidence"] = "the harness reads the raw bytes"
    value["representation_evidence"] = "the frame starts with its header"
    observed = "harness reads raw bytes; frame starts with a header"
    assert validate_readiness(value, observed)["ready"]


def test_observation_memory_survives_history_trimming(tmp_path):
    seen_histories = []

    def readiness_judge(skeleton, history, *args, **kwargs):
        seen_histories.append(history)
        if "harness reads raw bytes" not in history:
            return readiness(False)
        return readiness(True)

    supervisor = SemanticCandidateSupervisor(
        skeleton={},
        log_path=tmp_path / "semantic.jsonl",
        api_key="test",
        inject_message=lambda message: None,
        judge=lambda *args, **kwargs: decision("allow"),
        readiness_judge=readiness_judge,
    )
    assert supervisor.before_action(run_event("read harness"), "read harness", OBSERVED_HISTORY)
    assert supervisor.observation_memory
    supervisor.commit_required = False
    assert supervisor.before_action(
        run_event("inspect parser"),
        "inspect parser",
        "--BEGIN AGENT OBSERVATION--\nnew parser output\n--END AGENT OBSERVATION--",
    )
    assert "harness reads raw bytes" in seen_histories[-1]


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        test_semantic_redirect_has_no_counter_threshold(root)
        test_concrete_blocker_action_is_allowed(root)
        test_submission_bypasses_llm_but_revision_remains_gated(root)
        test_finish_redirect_does_not_block_trace_finalization(root)
        test_verified_target_success_is_terminal(root)
        test_non_crashing_submission_cannot_finish_early(root)
        test_not_ready_history_does_not_gate_proposed_action(root)
        test_observation_memory_survives_history_trimming(root)
    test_semantic_facts_compute_redirect_deterministically()
    test_no_concrete_blocker_text_is_normalized()
    test_readiness_requires_commitment()
    test_readiness_evidence_must_come_from_observation()
    test_readiness_accepts_grounded_brief_paraphrase()
