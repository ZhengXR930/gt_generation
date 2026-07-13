"""GT-blind, citation-grounded reasoning observer (evaluation harness).

Reconstructs the vulnerability reasoning a coding agent COMMITTED to, directly from
its trajectory prose — independent of whether the agent proactively called the
recorder. This closes the "recording-conformance confound": an agent that reasoned
well but recorded sparsely is no longer under-credited.

The observer is GROUND-TRUTH-BLIND on purpose: it never sees the GT, so it cannot
confirmation-fit to the answer. Scoring against the GT stays a separate downstream
step (the existing evaluator), which reduces the observer's record together with the
agent's own recorder records.

Pipeline:
    build_observer_input(traj)                      # deterministic: reasoning surface
      -> extract_trace(events, backend)             # LLM: nodes/edges + per-claim citations
      -> verify_citations(trace, events)            # GATE 1 deterministic: drop uncited/hallucinated
      -> skeptic_filter(kept, events, backend)      # GATE 2 LLM: drop explored-then-rejected
      -> trace_to_record(kept)                      # deterministic: recorder-format vulnerability_state
      -> recorder_fidelity(kept, recorder_state)    # deterministic: what the recorder missed

The LLM steps take an injected `backend: Callable[[str], str]`. `litellm_backend()`
provides the default (reusing the harness' permissioned model, which handles security
content); tests inject a stub, and demos can inject a pre-extracted trace.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from recorder_core.core import normalize_record, reduce_records, role_group

LLMBackend = Callable[[str], str]

VALID_EDGE_TYPES = {"data", "control", "order"}
_MIN_EVENT_CHARS = 30


# --------------------------------------------------------------------------- #
# 1. Deterministic: build the GT-blind observer input (the agent's own prose)  #
# --------------------------------------------------------------------------- #
def build_observer_input(trajectory: list) -> list[dict[str, Any]]:
    """Numbered reasoning surface (thoughts + messages), excluding the recorder
    calls themselves, so citations can be verified against event text later."""
    events: list[dict[str, Any]] = []
    for i, it in enumerate(trajectory):
        if not isinstance(it, dict) or it.get("source") != "agent":
            continue
        if it.get("action") == "record_reasoning":  # that IS the recorder, not raw context
            continue
        args = it.get("args") if isinstance(it.get("args"), dict) else {}
        parts = []
        for txt in (args.get("thought"), args.get("content"),
                    it.get("message") if it.get("action") == "message" else None):
            if isinstance(txt, str) and txt.strip():
                parts.append(txt.strip())
        text = "\n".join(dict.fromkeys(parts))
        if len(text) >= _MIN_EVENT_CHARS:
            events.append({"event_id": i, "action": it.get("action"), "text": text})
    return events


def render_input(events: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"[event {e['event_id']}] ({e['action']})\n{e['text']}" for e in events)


# --------------------------------------------------------------------------- #
# 2. Prompts                                                                   #
# --------------------------------------------------------------------------- #
OBSERVER_PROMPT = """\
You are a REASONING OBSERVER in a program-analysis evaluation harness. You are \
GROUND-TRUTH-BLIND: you do NOT know the correct answer and must NOT guess it. \
Report only the memory-safety reasoning THIS agent actually committed to.

Below is the agent's reasoning surface as numbered `[event N]` blocks:

{input}

Output ONLY a JSON object:
{{"nodes":[{{"role":"source|tainted_read|materialization|dispatch|alloc|free|root_cause|sink",
  "file":"...","function":"...","line":<int|null>,"var":"...","text":"<=1-line",
  "event_id":<N>,"quote":"<verbatim substring of event N>"}}],
 "edges":[{{"from":"<var>","to":"<var>","type":"data|control|order",
  "relation":"<free_before_use|double_free|missing_check|flows_to|...>",
  "event_id":<N>,"quote":"<verbatim substring of event N>"}}]}}

HARD RULES:
1. CITATION MANDATORY: every node/edge needs event_id + a VERBATIM quote copied \
exactly from that event. If you cannot cite verbatim, DROP it.
2. COMMITTED ONLY: include a claim only if the agent asserted it as its \
understanding. EXCLUDE hypotheses explored then rejected, and pure exploration.
3. Do NOT infer beyond the text; do NOT invent nodes the agent never stated. Fewer is fine.
4. Capture the MECHANISM (free chain / ordering / guards) as order/control edges.
5. `type` is EXACTLY one of data|control|order."""

SKEPTIC_PROMPT = """\
You are an adversarial verifier. For each claim below, decide if the agent truly \
COMMITTED to it as its understanding, or merely EXPLORED it (a hypothesis it raised \
then dropped, a question, or reading around). Be strict: default to "explored" if \
the quote does not clearly assert the claim.

Claims (with the verbatim quote the observer cited):
{claims}

Output ONLY: {{"verdicts":[{{"id":<i>,"committed":true|false,"why":"<short>"}}]}}"""


# --------------------------------------------------------------------------- #
# 3. LLM backend (injectable; litellm default reuses the harness model)        #
# --------------------------------------------------------------------------- #
def litellm_backend(model: str | None = None, api_key: str | None = None,
                    base_url: str | None = None, temperature: float = 0.0) -> LLMBackend:
    """Default backend via litellm. Model/key/url resolve from env when omitted:
    GT_OBSERVER_MODEL | OPENHANDS_REASONING_OBSERVER_MODEL, LLM_API_KEY, LLM_BASE_URL."""
    import litellm  # lazy: importing this module must not require litellm

    model = model or os.getenv("GT_OBSERVER_MODEL") or os.getenv(
        "OPENHANDS_REASONING_OBSERVER_MODEL") or "gpt-5.4"
    api_key = api_key or os.getenv("LLM_API_KEY")
    base_url = base_url or os.getenv("LLM_BASE_URL") or None

    def _call(prompt: str) -> str:
        resp = litellm.completion(
            model=model, api_key=api_key, base_url=base_url, temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp["choices"][0]["message"]["content"]

    return _call


def openai_backend(model: str, api_key: str, base_url: str | None = None,
                   temperature: float | None = 0.0) -> LLMBackend:
    """Backend for any OpenAI-compatible endpoint (OpenAI, a gateway, DeepSeek, ...).
    Uses the `openai` SDK. `temperature=None` omits the param (some reasoning models
    reject non-default temperature)."""
    from openai import OpenAI  # lazy
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    def _call(prompt: str) -> str:
        kw = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if temperature is not None:
            kw["temperature"] = temperature
        try:
            resp = client.chat.completions.create(**kw)
        except Exception:
            kw.pop("temperature", None)  # retry once without temperature
            resp = client.chat.completions.create(**kw)
        return resp.choices[0].message.content or ""

    return _call


def _parse_json(text: str) -> dict[str, Any]:
    """Tolerant extraction of the first JSON object from an LLM reply."""
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, depth, in_str, esc = text.find("{"), 0, False, False
    if start < 0:
        return {}
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            esc = (c == "\\") and not esc
            if c == '"' and not esc:
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


# --------------------------------------------------------------------------- #
# 4. Extraction + the two gates                                                #
# --------------------------------------------------------------------------- #
def extract_trace(events: list[dict[str, Any]], backend: LLMBackend) -> dict[str, Any]:
    out = _parse_json(backend(OBSERVER_PROMPT.format(input=render_input(events))))
    return {"nodes": list(out.get("nodes") or []), "edges": list(out.get("edges") or [])}


def verify_citations(trace: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """GATE 1 (deterministic): keep only items whose quote is a verbatim substring
    of the cited event's text, and whose edge type is valid."""
    by_ev = {e["event_id"]: e["text"] for e in events}

    def ok(item: dict[str, Any]) -> bool:
        ev, q = item.get("event_id"), item.get("quote")
        return isinstance(ev, int) and ev in by_ev and isinstance(q, str) and bool(q) and q in by_ev[ev]

    kept_nodes = [n for n in trace["nodes"] if isinstance(n, dict) and ok(n)]
    kept_edges = [e for e in trace["edges"] if isinstance(e, dict) and ok(e)
                  and str(e.get("type")) in VALID_EDGE_TYPES]
    dropped = ([{"kind": "node", **n} for n in trace["nodes"] if n not in kept_nodes]
               + [{"kind": "edge", **e} for e in trace["edges"] if e not in kept_edges])
    return {"nodes": kept_nodes, "edges": kept_edges, "dropped": dropped}


def skeptic_filter(trace: dict[str, Any], events: list[dict[str, Any]],
                   backend: LLMBackend) -> dict[str, Any]:
    """GATE 2 (LLM adversarial): drop claims the agent only EXPLORED, keeping
    committed ones. Fails safe: on parse failure, keep everything (gate 1 already held)."""
    items = trace["nodes"] + trace["edges"]
    if not items:
        return {"nodes": [], "edges": [], "rejected": []}
    claims = "\n".join(
        f'{i}: [{it.get("role") or it.get("type")}] "{it.get("quote")}"' for i, it in enumerate(items))
    verdicts = _parse_json(backend(SKEPTIC_PROMPT.format(claims=claims))).get("verdicts")
    if not isinstance(verdicts, list):
        return {"nodes": trace["nodes"], "edges": trace["edges"], "rejected": []}
    committed = {v.get("id") for v in verdicts if isinstance(v, dict) and v.get("committed")}
    keep = [items[i] for i in range(len(items)) if i in committed] if committed else items
    rej = [items[i] for i in range(len(items)) if i not in committed] if committed else []
    return {"nodes": [x for x in keep if x in trace["nodes"]],
            "edges": [x for x in keep if x in trace["edges"]], "rejected": rej}


# --------------------------------------------------------------------------- #
# 5. Deterministic: recorder-format record + fidelity diagnostic               #
# --------------------------------------------------------------------------- #
def _claim(n: dict[str, Any]) -> dict[str, Any]:
    return {"file": n.get("file"), "function": n.get("function"), "line": n.get("line"),
            "var": n.get("var"), "code": n.get("code") or "", "text": n.get("text") or "",
            "role": n.get("role"), "status": "confirmed", "evidence": "observer"}


def trace_to_record(trace: dict[str, Any], event_id: int = 10_000) -> dict[str, Any]:
    """A single recorder-compatible `vulnerability_state` record. `build_agent_state`
    reduces it exactly like an agent's own record, so scoring needs no evaluator change."""
    src, rc, snk = [], [], []
    for n in trace["nodes"]:
        grp = role_group(n.get("role"))
        (snk if grp == "sinks" else rc if grp == "root_causes" else src).append(_claim(n))
    edges = [{"from": e.get("from"), "to": e.get("to"), "type": e.get("type"),
              "relation": e.get("relation") or "", "status": "confirmed"} for e in trace["edges"]]
    return {"kind": "vulnerability_state", "status": "confirmed", "confidence": "high",
            "stage": "observer", "source": "observer", "event_id": event_id,
            "text": "reconstructed by GT-blind observer",
            "sources": src, "root_causes": rc, "sinks": snk, "edges": edges}


def _node_key(n: dict[str, Any]) -> tuple:
    return (str(n.get("function") or "").lower(), n.get("line"))


def recorder_fidelity(trace: dict[str, Any], recorder_state: dict[str, Any] | None) -> dict[str, Any]:
    """What the observer captured that the recorder did NOT (the confound recovered)."""
    rec_nodes = (recorder_state or {}).get("all_nodes") or []
    rec_keys = {_node_key(n) for n in rec_nodes}
    rec_edges = (recorder_state or {}).get("trace") or []
    rec_edge_keys = {(str(e.get("from") or "").lower(), str(e.get("to") or "").lower(),
                      str(e.get("type") or "")) for e in rec_edges}
    node_only = [n for n in trace["nodes"] if _node_key(n) not in rec_keys]
    edge_only = [e for e in trace["edges"]
                 if (str(e.get("from") or "").lower(), str(e.get("to") or "").lower(),
                     str(e.get("type") or "")) not in rec_edge_keys]
    return {
        "recorder_nodes": len(rec_nodes), "recorder_edges": len(rec_edges),
        "observer_nodes": len(trace["nodes"]), "observer_edges": len(trace["edges"]),
        "observer_only_nodes": node_only, "observer_only_edges": edge_only,
        "recovered_nodes": len(node_only), "recovered_edges": len(edge_only),
    }


# --------------------------------------------------------------------------- #
# 6. Orchestration                                                             #
# --------------------------------------------------------------------------- #
def run_observer(trajectory_path: Path, out_dir: Path, *, backend: LLMBackend | None = None,
                 recorder_state: dict[str, Any] | None = None, skeptic: bool = True,
                 pre_extracted_trace: dict[str, Any] | None = None) -> dict[str, Any]:
    """End-to-end. Provide `backend` (LLM) OR `pre_extracted_trace` (raw nodes/edges,
    e.g. for tests/replay). Writes observer_trace.json, observed_reasoning_events.jsonl,
    recorder_fidelity.json into out_dir. Returns a summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    trajectory = json.loads(Path(trajectory_path).read_text(encoding="utf-8", errors="replace"))
    events = build_observer_input(trajectory)

    if pre_extracted_trace is not None:
        raw = {"nodes": list(pre_extracted_trace.get("nodes") or []),
               "edges": list(pre_extracted_trace.get("edges") or [])}
    elif backend is not None:
        raw = extract_trace(events, backend)
    else:
        raise ValueError("run_observer needs either backend= or pre_extracted_trace=")

    verified = verify_citations(raw, events)
    if skeptic and backend is not None:
        kept = skeptic_filter(verified, events, backend)
        kept["dropped"] = verified["dropped"]
    else:
        kept = {**verified, "rejected": []}

    record = trace_to_record(kept)
    fidelity = recorder_fidelity(kept, recorder_state)

    (out_dir / "observer_trace.json").write_text(
        json.dumps({"nodes": kept["nodes"], "edges": kept["edges"],
                    "dropped": kept.get("dropped", []), "rejected": kept.get("rejected", [])},
                   ensure_ascii=False, indent=2))
    (out_dir / "observed_reasoning_events.jsonl").write_text(
        json.dumps(normalize_record(record), ensure_ascii=False) + "\n")
    (out_dir / "recorder_fidelity.json").write_text(json.dumps(fidelity, ensure_ascii=False, indent=2))

    return {"input_events": len(events), "nodes": len(kept["nodes"]), "edges": len(kept["edges"]),
            "citations_dropped": len(verified["dropped"]),
            "skeptic_rejected": len(kept.get("rejected", [])), "fidelity": fidelity}


def observed_state(out_dir: Path) -> dict[str, Any]:
    """Reduce the observer's emitted record into a reasoning_state (recorder-identical).
    NOTE: the recorder reduction drops claims that lack file/line (its completeness gate),
    so prose-level observer nodes are lost here — use `merge_observer_into_agent` for
    scoring, which keeps function-level observer nodes/edges."""
    path = out_dir / "observed_reasoning_events.jsonl"
    if not path.exists():
        return {}
    recs = [normalize_record(json.loads(ln)) for ln in path.read_text().splitlines() if ln.strip()]
    return reduce_records(recs) if recs else {}


# --------------------------------------------------------------------------- #
# 7. Scoring integration: merge observer nodes/edges into agent state          #
#    (function-level allowed — the recorder completeness gate does NOT apply    #
#     to the observer, whose reasoning is reconstructed from prose)            #
# --------------------------------------------------------------------------- #
def load_observer_trace(path: Path) -> dict[str, Any]:
    p = Path(path)
    if p.is_dir():
        p = p / "observer_trace.json"
    if not p.exists():
        return {"nodes": [], "edges": []}
    d = json.loads(p.read_text(encoding="utf-8"))
    return {"nodes": list(d.get("nodes") or []), "edges": list(d.get("edges") or [])}


def merge_observer_into_agent(agent: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    """Union observer nodes/edges into an agent state, deduped. Observer items already
    carry role/function/line/var and type/relation, which is exactly what the matchers
    read; edges match by variable so they score even when nodes are function-level."""
    def nkey(n):
        return (str(n.get("function") or "").lower(), n.get("line"),
                str(n.get("var") or "").lower(), str(n.get("role") or "").lower())

    def ekey(e):
        return (str(e.get("from") or "").lower(), str(e.get("to") or "").lower(), str(e.get("type") or ""))

    seen = {nkey(n) for n in agent.get("nodes", [])}
    for n in trace.get("nodes", []):
        if nkey(n) not in seen:
            agent["nodes"].append({"role": n.get("role"), "file": n.get("file"),
                                   "function": n.get("function"), "line": n.get("line"),
                                   "var": n.get("var"), "text": n.get("text"), "source": "observer"})
            seen.add(nkey(n))
    eseen = {ekey(e) for e in agent.get("edges", [])}
    for e in trace.get("edges", []):
        if ekey(e) not in eseen:
            agent["edges"].append({"from": e.get("from"), "to": e.get("to"), "type": e.get("type"),
                                   "relation": e.get("relation"), "source": "observer"})
            eseen.add(ekey(e))
    return agent


def _cli(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="GT-blind citation-grounded reasoning observer")
    ap.add_argument("--trajectory", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--no-skeptic", action="store_true")
    ap.add_argument("--trace", type=Path, help="pre-extracted nodes/edges JSON (skip LLM)")
    ap.add_argument("--recorder-state", type=Path, help="recorder reasoning_state.json for fidelity")
    ns = ap.parse_args(argv)
    pre = json.loads(ns.trace.read_text()) if ns.trace else None
    rec = json.loads(ns.recorder_state.read_text()) if ns.recorder_state else None
    backend = None if pre is not None else litellm_backend()
    summary = run_observer(ns.trajectory, ns.out_dir, backend=backend, recorder_state=rec,
                           skeptic=not ns.no_skeptic, pre_extracted_trace=pre)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
