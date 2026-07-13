"""Deterministic tests for the GT-blind citation-grounded observer.

No network / LLM: the extraction step is exercised via an injected stub backend,
and the two gates + integration are pure functions.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "evaluation_mode"))

from external_interpreter.observer import (  # noqa: E402
    build_observer_input, verify_citations, skeptic_filter, trace_to_record,
    recorder_fidelity, merge_observer_into_agent, run_observer,
    build_evidence_bank, evidence_digest, ground_trace, injection_meta_eval,
    canon_var, canon_relation, align_claim_to_gt, understanding_score, gt_mechanism,
)


def _traj():
    return [
        {"source": "agent", "action": "read", "args": {"thought": "Let me look at free_packet."}},
        {"source": "agent", "action": "message",
         "message": "The crash is a use-after-free: free_packet frees pt then proc_plaintext reads it."},
        {"source": "agent", "action": "record_reasoning", "args": {"thought": "recording now"}},  # excluded
        {"source": "user", "action": "message", "message": "ignore me"},                          # non-agent
    ]


def test_build_input_excludes_recorder_and_nonagent():
    ev = build_observer_input(_traj())
    assert [e["event_id"] for e in ev] == [1]          # event 0 too short(<30), 2 recorder, 3 non-agent
    assert "use-after-free" in ev[0]["text"]


def test_verify_citations_drops_hallucinated_and_bad_type():
    events = [{"event_id": 1, "text": "free_packet frees pt then proc_plaintext reads it"}]
    trace = {
        "nodes": [
            {"role": "free", "function": "free_packet", "event_id": 1, "quote": "free_packet frees pt"},
            {"role": "sink", "function": "x", "event_id": 1, "quote": "NOT IN THE TEXT"},        # drop
            {"role": "sink", "function": "y", "event_id": 9, "quote": "free_packet"},            # bad event
        ],
        "edges": [
            {"from": "pt", "to": "pt", "type": "order", "event_id": 1, "quote": "frees pt then proc_plaintext reads it"},
            {"from": "a", "to": "b", "type": "bogus", "event_id": 1, "quote": "free_packet"},    # bad type
        ],
    }
    kept = verify_citations(trace, events)
    assert len(kept["nodes"]) == 1 and kept["nodes"][0]["function"] == "free_packet"
    assert len(kept["edges"]) == 1 and kept["edges"][0]["type"] == "order"
    assert len(kept["dropped"]) == 3


def test_skeptic_filter_drops_explored():
    events = [{"event_id": 1, "text": "committed claim. explored claim."}]
    trace = {"nodes": [{"role": "free", "quote": "committed claim"},
                       {"role": "sink", "quote": "explored claim"}], "edges": []}

    def stub(prompt: str) -> str:
        return json.dumps({"verdicts": [{"id": 0, "committed": True}, {"id": 1, "committed": False}]})

    out = skeptic_filter(trace, events, stub)
    assert len(out["nodes"]) == 1 and out["nodes"][0]["role"] == "free"
    assert len(out["rejected"]) == 1


def test_trace_to_record_routes_by_role_group():
    trace = {"nodes": [{"role": "source", "function": "s"}, {"role": "root_cause", "function": "r"},
                       {"role": "sink", "function": "k"}, {"role": "free", "function": "f"}],
             "edges": [{"from": "a", "to": "b", "type": "order", "relation": "free_before_use"}]}
    rec = trace_to_record(trace)
    assert rec["kind"] == "vulnerability_state"
    assert [c["function"] for c in rec["sinks"]] == ["k"]
    assert any(c["function"] == "r" for c in rec["root_causes"])
    assert rec["edges"][0]["type"] == "order"


def test_recorder_fidelity_counts_recovery():
    trace = {"nodes": [{"function": "sdb_free", "line": None}, {"function": "ns_free", "line": None}],
             "edges": [{"from": "s", "to": "s->ht", "type": "order"}]}
    recorder = {"all_nodes": [{"function": "sdb_free", "line": 217}], "trace": []}
    fid = recorder_fidelity(trace, recorder)
    assert fid["recovered_edges"] == 1
    # ns_free not in recorder -> recovered; sdb_free line differs -> counted as recovered (function+line key)
    assert fid["recovered_nodes"] == 2


def test_merge_observer_into_agent_unions_and_dedups():
    agent = {"nodes": [{"function": "sdb_free", "line": 217, "var": "s", "role": "sink"}],
             "edges": []}
    trace = {"nodes": [{"function": "ns_free", "line": None, "var": "ns->sdb", "role": "free"}],
             "edges": [{"from": "s", "to": "s->ht", "type": "order", "relation": "free_before_use"}]}
    merge_observer_into_agent(agent, trace)
    assert len(agent["nodes"]) == 2
    assert len(agent["edges"]) == 1 and agent["edges"][0]["type"] == "order"
    # idempotent
    merge_observer_into_agent(agent, trace)
    assert len(agent["nodes"]) == 2 and len(agent["edges"]) == 1


def _traj_with_reads():
    # content uses the REAL OpenHands formats: `cat -n` -> "NNNN\t<code>", grep -n -> "NNNN:<code>"
    return [
        {"source": "agent", "action": "read", "id": "a1",
         "args": {"path": "src/bitreader.c", "thought": "check the loop"}},
        {"source": "observation", "cause": "a1", "observation": "read",
         "content": "Here's the result of running `cat -n` on src/bitreader.c:\n"
                    "   866\tstatic int read_rice_signed_block(BitReader* br) {\n"
                    "   867\t    b = br->buffer[cwords];\n"},
        {"source": "agent", "action": "run", "id": "a2",
         "args": {"command": "grep -n cwords src/bitreader.c"}},
        {"source": "observation", "cause": "a2", "observation": "run",
         "content": "867:    b = br->buffer[cwords];"},
    ]


def test_build_evidence_bank_and_digest():
    bank = build_evidence_bank(_traj_with_reads())
    assert "bitreader.c" in bank["files"]
    assert ("bitreader.c", 867) in bank["locations"]     # from read range AND grep hit
    dig = evidence_digest(bank)
    assert "bitreader.c" in dig


def test_ground_trace_flags_ungrounded():
    bank = build_evidence_bank(_traj_with_reads())
    trace = {"nodes": [
        {"role": "sink", "file": "bitreader.c", "line": 867},       # grounded (file + line viewed)
        {"role": "root_cause", "file": "bitreader.c", "line": 999},  # file ok, line not viewed
        {"role": "source", "file": "never_read.c", "line": 5},       # ungrounded file
    ], "edges": []}
    stats = ground_trace(trace, bank)
    assert trace["nodes"][0]["grounded_in_reads"] and trace["nodes"][0]["line_in_viewed_range"]
    assert trace["nodes"][1]["grounded_in_reads"] and not trace["nodes"][1]["line_in_viewed_range"]
    assert not trace["nodes"][2]["grounded_in_reads"]
    assert stats["grounded_nodes"] == 2


def test_injection_meta_eval_catches_fakes():
    events = build_observer_input(_traj_with_reads()) or [{"event_id": 0, "action": "x", "text": "hi"}]
    bank = build_evidence_bank(_traj_with_reads())
    m = injection_meta_eval(events, bank, n=10)
    assert m["citation_detection_rate"] == 1.0      # fabricated quotes all dropped
    assert m["grounding_detection_rate"] == 1.0     # ungrounded files all flagged


def test_canon_var_and_relation():
    assert canon_var("br->buffer[cwords]") >= {"buffer", "cwords"}
    assert canon_var("hb_vector_t<CFF::OpStr, 8u>::fini") == {"hb_vector_t", "fini"}
    assert canon_relation("double-fini") == "double_free"
    assert canon_relation("use-after-free") == "uaf"
    assert canon_relation("bounds_check") == "oob"


def test_align_claim_matches_via_object_raw_and_site_alias():
    gt = {"kind": "bounds_check", "variable": "cwords", "region_function": "f"}
    a = align_claim_to_gt([{"relation": "oob_read", "object": "buffer",
                            "object_raw": "br->buffer[cwords]"}], gt)
    assert a["relation_match"] and a["object_match"]           # cwords found via object_raw
    # lifetime alias: agent names entry->cleanupCallback; GT object dc + site sentry->cleanupCallback
    gt2 = {"kind": "lifetime", "object": "dc", "relation": "free_before_use",
           "sites": [{"var": "sentry->cleanupCallback"}]}
    a2 = align_claim_to_gt([{"relation": "use_after_free", "object": "entry->cleanupCallback"}], gt2)
    assert a2["relation_match"] and a2["object_match"]


def test_understanding_score_gating_without_backend():
    gt = {"kind": "lifetime", "object": "arrayZ_", "relation": "double_free", "sites": [{"var": "arrayZ_"}]}
    assert understanding_score([{"relation": "double_free", "object": "arrayZ_"}], gt, None)["score"] == 0.4
    assert understanding_score([{"relation": "double_free", "object": "foo"}], gt, None)["score"] == 0.2
    assert understanding_score([{"relation": "oob_read", "object": "arrayZ_"}], gt, None)["score"] == 0.0
    assert understanding_score([], gt, None)["score"] == 0.0


def test_understanding_score_mechanism_with_stub_backend():
    gt = {"kind": "lifetime", "object": "arrayZ_", "relation": "double_free",
          "sites": [{"var": "arrayZ_"}], "mechanism": "static_array aliasing"}
    claim = [{"relation": "double_free", "object": "arrayZ_", "object_raw": "arrayZ_",
              "mechanism": "aliasing duplicates owner", "mechanism_quote": "aliasing"}]
    match = understanding_score(claim, gt, lambda p: '{"verdict":"match","why":"same"}')
    assert match["score"] == 1.0 and match["mechanism_verdict"] == "match"
    miss = understanding_score(claim, gt, lambda p: '{"verdict":"mismatch","why":"different"}')
    assert miss["score"] == 0.4 and miss["band"] == "right_what_wrong_why"


def test_gt_mechanism_strings():
    assert "double_free" in gt_mechanism({"kind": "lifetime", "object": "x", "relation": "double_free", "sites": []})
    assert "bounds check" in gt_mechanism({"kind": "bounds_check", "variable": "i", "condition": "i<n"})


def test_run_observer_end_to_end_with_pre_extracted_trace(tmp_path):
    traj = tmp_path / "trajectory"
    traj.write_text(json.dumps(_traj()))
    pre = {"nodes": [{"role": "free", "function": "free_packet", "event_id": 1,
                      "quote": "free_packet frees pt"}],
           "edges": [{"from": "pt", "to": "pt", "type": "order", "relation": "free_before_use",
                      "event_id": 1, "quote": "frees pt then proc_plaintext reads it"}]}
    summary = run_observer(traj, tmp_path / "out", pre_extracted_trace=pre,
                           recorder_state={"all_nodes": [], "trace": []}, skeptic=False)
    assert summary["nodes"] == 1 and summary["edges"] == 1
    assert summary["fidelity"]["recovered_edges"] == 1
    assert (tmp_path / "out" / "observer_trace.json").exists()
    assert (tmp_path / "out" / "recorder_fidelity.json").exists()
