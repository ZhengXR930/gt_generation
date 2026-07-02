from recorder_core.core import append_reasoning_record, load_reasoning_events


def _snapshot(*, stage: str = "pre_submit", edge_count: int = 1) -> dict:
    edges = [
        {
            "from": f"input{i}",
            "to": f"value{i}",
            "relation": "parse",
            "file": "parser.c",
            "function": "parse",
            "line": 10 + i,
            "code": f"value{i} = input{i};",
            "note": f"edge {i}",
            "evidence": "code",
        }
        for i in range(edge_count)
    ]
    return {
        "kind": "vulnerability_state",
        "status": "confirmed",
        "stage": stage,
        "confidence": "high",
        "text": "complete vulnerability state",
        "sources": [
            {
                "file": "parser.c",
                "function": "parse",
                "line": 10,
                "var": "input",
                "code": "input = read_byte();",
                "note": "project parser reads attacker input",
                "evidence": "code",
            }
        ],
        "root_causes": [
            {
                "file": "parser.c",
                "function": "parse",
                "line": 20,
                "var": "len",
                "code": "memcpy(dst, src, len);",
                "note": "missing bounds check",
                "evidence": "code",
            }
        ],
        "edges": edges,
        "sinks": [
            {
                "file": "parser.c",
                "function": "parse",
                "line": 21,
                "var": "dst",
                "code": "dst[i] = src[i];",
                "note": "out-of-bounds write",
                "evidence": "code",
            }
        ],
        "open_questions": [],
    }


def test_complete_snapshot_without_new_facts_is_rejected_after_complete_state(tmp_path):
    events = tmp_path / "reasoning_events.jsonl"
    state = tmp_path / "reasoning_state.json"

    first = append_reasoning_record(_snapshot(), events_path=events, state_path=state)
    second = append_reasoning_record(_snapshot(), events_path=events, state_path=state)

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert "complete vulnerability_state already exists" in second["content"]
    assert len(load_reasoning_events(events)) == 1


def test_complete_snapshot_with_more_facts_is_rejected_without_revision_stage(tmp_path):
    events = tmp_path / "reasoning_events.jsonl"
    state = tmp_path / "reasoning_state.json"

    first = append_reasoning_record(_snapshot(edge_count=1), events_path=events, state_path=state)
    second = append_reasoning_record(_snapshot(edge_count=2), events_path=events, state_path=state)

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert len(load_reasoning_events(events)) == 1


def test_revision_snapshot_is_accepted_after_complete_state(tmp_path):
    events = tmp_path / "reasoning_events.jsonl"
    state = tmp_path / "reasoning_state.json"

    first = append_reasoning_record(_snapshot(), events_path=events, state_path=state)
    revision = append_reasoning_record(
        _snapshot(stage="revision"),
        events_path=events,
        state_path=state,
    )

    assert first["accepted"] is True
    assert revision["accepted"] is True
    assert len(load_reasoning_events(events)) == 2
