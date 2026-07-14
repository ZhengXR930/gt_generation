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
# 1b. Evidence bank — MECHANICAL record of what the agent actually SAW         #
#     (read/grep tools + their outputs). Used to GROUND the LLM extraction so  #
#     the model can only anchor reasoning to code the agent genuinely looked   #
#     at — shrinking the LLM's job toward labelling, not free invention.       #
# --------------------------------------------------------------------------- #
_SRC_EXT = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp")
_CATN_HEADER = re.compile(r"cat -n`? on (\S+)", re.I)          # read tool: "...cat -n on /path/foo.c:"
_GREP_PATH = re.compile(r"\b(\S+\.(?:c|cc|cpp|cxx|h|hh|hpp))\b", re.I)
_LINE_TAB = re.compile(r"(?m)^\s*(\d+)\t")                     # read/cat -n:  "1980\t<code>"
_LINE_COLON = re.compile(r"(?m)^\s*(\d+):(?!:)")              # grep -n:      "1988:<code>"
_FUNC_ON_LINE = re.compile(r"\b([A-Za-z_]\w{2,})\s*\(")       # any funcname( on a numbered line
_NOT_FUNC = {"if", "for", "while", "switch", "return", "sizeof", "static", "int", "void"}


def _observations_by_cause(trajectory: list) -> dict[Any, dict]:
    obs: dict[Any, dict] = {}
    for it in trajectory:
        if isinstance(it, dict) and "observation" in it and it.get("cause") is not None:
            obs.setdefault(it.get("cause"), it)
    return obs


def build_evidence_bank(trajectory: list) -> dict[str, Any]:
    """MECHANICAL record of what the agent actually SAW: which source files it read,
    which (file,line) it viewed (from `cat -n`/`grep -n` output), and where each
    function it saw is defined. No LLM. Handles the real OpenHands output formats
    (`NNNN\\t<code>` for reads, `NNNN:<code>` for grep -n)."""
    obs = _observations_by_cause(trajectory)
    files: set[str] = set()
    locations: set[tuple[str, int]] = set()
    func_file: dict[str, str] = {}          # funcname -> basename it was seen in
    for it in trajectory:
        if not isinstance(it, dict) or it.get("source") != "agent":
            continue
        args = it.get("args") if isinstance(it.get("args"), dict) else {}
        o = obs.get(it.get("id")) or {}
        content = str(o.get("content") or o.get("message") or "")
        path, cmd = str(args.get("path") or ""), str(args.get("command") or "")
        # which source file is this content about?
        base = ""
        mh = _CATN_HEADER.search(content)
        if mh and mh.group(1).endswith(_SRC_EXT):
            base = mh.group(1).split("/")[-1]
        elif path.endswith(_SRC_EXT):
            base = path.split("/")[-1]
        else:
            mg = _GREP_PATH.search(cmd)
            if mg:
                base = mg.group(1).split("/")[-1]
        if base:
            files.add(base)
        for line, is_tab in ([(m, True) for m in _LINE_TAB.finditer(content)]
                             + [(m, False) for m in _LINE_COLON.finditer(content)]):
            ln = int(line.group(1))
            row = content[line.end():content.find("\n", line.end()) if content.find("\n", line.end()) > 0 else None]
            if base:
                locations.add((base, ln))
                for fm in _FUNC_ON_LINE.finditer(row):
                    fn = fm.group(1)
                    if fn not in _NOT_FUNC:
                        func_file.setdefault(fn, base)
    return {"files": files, "locations": locations, "func_file": func_file}


def evidence_digest(bank: dict[str, Any], max_files: int = 50) -> str:
    byf: dict[str, list[int]] = {}
    for f, ln in bank.get("locations", set()):
        byf.setdefault(f, []).append(ln)
    files = sorted(bank.get("files", set()))[:max_files]
    ranges = "; ".join(f"{f}:{min(ls)}-{max(ls)}" for f, ls in sorted(byf.items())[:max_files])
    funcs = sorted(bank.get("func_file", {}).items())[:60]
    fmap = ", ".join(f"{fn}()->{fb}" for fn, fb in funcs)
    return (f"Files the agent read: {', '.join(files) or '(none)'}\n"
            f"Line ranges it viewed: {ranges or '(none)'}\n"
            f"Functions it saw (name->file): {fmap or '(none)'}")


def _resolve_file(node: dict[str, Any], func_file: dict[str, str]) -> str:
    f = str(node.get("file") or "").split("/")[-1].lower()
    if f and f != "unknown":
        return f
    fn = str(node.get("function") or "")
    return str(func_file.get(fn, "")).lower()          # resolve file from the function name


def ground_trace(trace: dict[str, Any], bank: dict[str, Any]) -> dict[str, Any]:
    """Annotate each node with whether it is grounded in what the agent actually
    read. The file is resolved from node.file OR (since agents name functions, not
    files) from the function->file map. A node whose location the agent never viewed
    is flagged (likely hallucinated / mis-extracted)."""
    files = {f.lower() for f in bank.get("files", set())}
    func_file = {k: v.lower() for k, v in bank.get("func_file", {}).items()}
    byf: dict[str, list[int]] = {}
    for f, ln in bank.get("locations", set()):
        byf.setdefault(f.lower(), []).append(ln)
    grounded = 0
    for n in trace.get("nodes", []):
        rf = _resolve_file(n, func_file)
        fn_ok = bool(rf) and rf in files
        ln = n.get("line")
        line_ok = bool(fn_ok and isinstance(ln, int) and byf.get(rf)
                       and any(abs(ln - x) <= 3 for x in byf[rf]))
        n["grounded_in_reads"] = fn_ok
        n["line_in_viewed_range"] = line_ok
        grounded += fn_ok
    total = len(trace.get("nodes", []))
    return {"nodes": total, "grounded_nodes": grounded,
            "grounded_ratio": round(grounded / total, 3) if total else None}


# --------------------------------------------------------------------------- #
# 2. Prompts                                                                   #
# --------------------------------------------------------------------------- #
OBSERVER_PROMPT = """\
You are a REASONING OBSERVER in a program-analysis evaluation harness. You are \
GROUND-TRUTH-BLIND: you do NOT know the correct answer and must NOT guess it. \
Report only the memory-safety reasoning THIS agent actually committed to.

Below is the agent's reasoning surface as numbered `[event N]` blocks:

{input}

EVIDENCE the agent actually read (use it to fill file/line — prefer real values from
here; if the agent only named a function and a line, give `function` + `line` and set
`file` to the matching one from the map, else null. NEVER write "unknown"):
{evidence}

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


def azure_backend(model: str, api_key: str, azure_endpoint: str,
                  api_version: str = "2024-03-01-preview", temperature: float | None = None) -> LLMBackend:
    """Backend for an Azure-OpenAI-style gateway (e.g. the ByteDance modelhub/AIDP
    endpoint used for gpt-5.x). `temperature=None` by default — gpt-5 reasoning models
    reject a custom temperature."""
    from openai import AzureOpenAI  # lazy
    client = AzureOpenAI(api_key=api_key, azure_endpoint=azure_endpoint, api_version=api_version)

    def _call(prompt: str) -> str:
        kw = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if temperature is not None:
            kw["temperature"] = temperature
        try:
            resp = client.chat.completions.create(**kw)
        except Exception:
            kw.pop("temperature", None)
            resp = client.chat.completions.create(**kw)
        return resp.choices[0].message.content or ""

    return _call


def backend_from_config(config_path: str = "config.txt") -> LLMBackend | None:
    """Build the default observer backend (gpt-5.5 via the AIDP/modelhub gateway) from
    config.txt's OPENAI_API_KEY (the aidp_ak). Returns None if unavailable — callers
    must treat a None backend as 'mechanism judging deferred', never fatal."""
    import re as _re
    from pathlib import Path as _Path
    model = os.getenv("GT_OBSERVER_MODEL", "gpt-5.5-2026-04-24")
    endpoint = os.getenv("GT_OBSERVER_AZURE_ENDPOINT",
                         "https://aidp.bytedance.net/api/modelhub/online/v2/crawl")
    key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        try:
            m = _re.search(r'OPENAI_API_KEY\s*[=:]\s*"?([^"\n]+)"?', _Path(config_path).read_text())
            key = m.group(1).strip() if m else None
        except OSError:
            key = None
    if not key:
        return None
    try:
        return azure_backend(model, key, endpoint)
    except Exception:
        return None


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
def extract_trace(events: list[dict[str, Any]], backend: LLMBackend,
                  evidence: str = "(none)") -> dict[str, Any]:
    out = _parse_json(backend(OBSERVER_PROMPT.format(input=render_input(events), evidence=evidence)))
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
    bank = build_evidence_bank(trajectory)

    if pre_extracted_trace is not None:
        raw = {"nodes": list(pre_extracted_trace.get("nodes") or []),
               "edges": list(pre_extracted_trace.get("edges") or [])}
    elif backend is not None:
        raw = extract_trace(events, backend, evidence_digest(bank))
    else:
        raise ValueError("run_observer needs either backend= or pre_extracted_trace=")

    verified = verify_citations(raw, events)          # GATE 1: no fabricated citations
    if skeptic and backend is not None:
        kept = skeptic_filter(verified, events, backend)   # GATE 2: committed, not explored
        kept["dropped"] = verified["dropped"]
    else:
        kept = {**verified, "rejected": []}
    grounding = ground_trace(kept, bank)              # GATE 3: grounded in what it read

    record = trace_to_record(kept)
    fidelity = recorder_fidelity(kept, recorder_state)

    (out_dir / "observer_trace.json").write_text(
        json.dumps({"nodes": kept["nodes"], "edges": kept["edges"],
                    "dropped": kept.get("dropped", []), "rejected": kept.get("rejected", []),
                    "grounding": grounding}, ensure_ascii=False, indent=2))
    (out_dir / "observed_reasoning_events.jsonl").write_text(
        json.dumps(normalize_record(record), ensure_ascii=False) + "\n")
    (out_dir / "recorder_fidelity.json").write_text(json.dumps(fidelity, ensure_ascii=False, indent=2))

    return {"input_events": len(events), "nodes": len(kept["nodes"]), "edges": len(kept["edges"]),
            "evidence_files": len(bank["files"]),
            "citations_dropped": len(verified["dropped"]),
            "skeptic_rejected": len(kept.get("rejected", [])),
            "grounding": grounding, "fidelity": fidelity}


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
# 6b. Injection meta-evaluation — MEASURE the precision defence (à la TRACE):  #
#     inject synthetic fabricated / ungrounded claims and confirm the          #
#     deterministic gates reject or flag them. Turns "faithfulness" into a      #
#     number instead of an assertion.                                          #
# --------------------------------------------------------------------------- #
def injection_meta_eval(events: list[dict[str, Any]], bank: dict[str, Any], n: int = 20) -> dict[str, Any]:
    ev0 = events[0]["event_id"] if events else 0
    fabricated = {"nodes": [{"role": "root_cause", "file": "x.c", "function": "f", "line": 1,
                             "event_id": ev0, "quote": f"__INJECTED_NONEXISTENT_QUOTE_{i}__"}
                            for i in range(n)], "edges": []}
    kept = verify_citations(fabricated, events)                 # GATE 1 must drop all
    cite_caught = n - len(kept["nodes"])
    ungrounded = {"nodes": [{"role": "sink", "file": f"never_read_{i}.c", "function": "g", "line": 9,
                             "event_id": ev0, "quote": ""} for i in range(n)], "edges": []}
    ground_trace(ungrounded, bank)                              # GATE 3 must flag all
    ground_flagged = sum(1 for x in ungrounded["nodes"] if not x.get("grounded_in_reads"))
    return {"citation_injections": n, "citation_caught": cite_caught,
            "citation_detection_rate": round(cite_caught / n, 3) if n else None,
            "grounding_injections": n, "grounding_flagged": ground_flagged,
            "grounding_detection_rate": round(ground_flagged / n, 3) if n else None}


# --------------------------------------------------------------------------- #
# 6c. Invariant-shaped reasoning record — align the RECORD to the frozen GT     #
#     invariant ({relation, object, mechanism}), instead of a path graph.       #
#     Matching then compares invariant-to-invariant: object (canonicalized) +   #
#     relation (controlled vocab), NO function/line/path string matching.       #
# --------------------------------------------------------------------------- #
_VAR_STOP = {"struct", "const", "unsigned", "signed", "int", "char", "void", "static",
             "long", "short", "the", "this", "return", "sizeof"}
_REL_FAMILY = {
    "double_free": "double_free", "doublefree": "double_free", "freed_twice": "double_free",
    "double_fini": "double_free", "free_free": "double_free",
    "free_before_use": "uaf", "use_after_free": "uaf", "uaf": "uaf", "read_after_free": "uaf",
    "write_after_free": "uaf", "use_before_free": "uaf",
    "oob_read": "oob", "out_of_bounds_read": "oob", "buffer_overflow_read": "oob",
    "heap_buffer_overflow_read": "oob", "oob": "oob", "oob_write": "oob", "bounds_check": "oob",
    "out_of_bounds_write": "oob", "buffer_overflow_write": "oob", "missing_bounds_check": "oob",
    "missing_check": "oob", "overflow": "oob", "underflow": "oob", "index_oob": "oob",
    "uninit_use": "uninit", "uninitialized": "uninit", "use_of_uninitialized": "uninit",
    "uninit": "uninit", "null_deref": "null_deref", "null_dereference": "null_deref",
}


def canon_var(v: Any) -> set[str]:
    """Canonicalize an object/variable to a token set: drop template args, then keep
    identifier tokens (>=2 chars). `br->buffer[cwords]` -> {br,buffer,cwords};
    `fontDicts.values.arrayZ_` -> {fontdicts,values,arrayz_}; `hb_vector_t<..>::fini`
    -> {hb_vector_t,fini}. Match = non-empty intersection, never string ==."""
    s = re.sub(r"<[^>]*>", "", str(v or ""))
    return {t.lower() for t in re.findall(r"[A-Za-z_]\w+", s) if t.lower() not in _VAR_STOP}


def canon_relation(r: Any) -> str:
    """Map a relation/kind to a canonical family (double_free | uaf | oob | uninit | ...)."""
    k = re.sub(r"[^a-z_]", "", str(r or "").lower().replace("-", "_").replace(" ", "_"))
    return _REL_FAMILY.get(k, k or "other")


INVARIANT_CLAIM_PROMPT = """\
You are analyzing a coding agent's reasoning about a C/C++ memory-safety bug. Extract the
INVARIANT the agent COMMITTED to — the core memory-safety violation it concluded — as JSON.
You are GROUND-TRUTH-BLIND; report only what the agent concluded.

Agent reasoning (numbered [event N]):
{input}

Evidence it read (for grounding file/var):
{evidence}

Output ONLY:
{{"claims": [
  {{"relation": "double_free|free_before_use|use_after_free|oob_read|oob_write|uninit_use|null_deref|other",
    "object": "<bare program variable that is mismanaged, e.g. cwords / arrayZ_ / dc — NO -> [] casts>",
    "object_raw": "<how the agent referred to it>",
    "sites": [{{"role":"alloc|free|first_free|second_free|use|guard|root_cause",
               "function":"...","var":"...","event_id":<N>,"quote":"<verbatim from event N>"}}],
    "mechanism": "<one sentence: WHY the violation happens (the root cause)>",
    "mechanism_quote": "<verbatim span from an event supporting the mechanism>",
    "event_id": <N>}}
]}}
RULES: committed only (not explored-then-dropped); `object` is the bare variable; every quote
VERBATIM from its event; if the agent committed to no clear invariant, return {{"claims": []}}."""


def _quote_ok(item: dict, by_ev: dict) -> bool:
    ev, q = item.get("event_id"), item.get("quote") or item.get("mechanism_quote")
    return isinstance(ev, int) and ev in by_ev and isinstance(q, str) and bool(q) and q in by_ev[ev]


def extract_invariant_claims(events: list[dict[str, Any]], backend: LLMBackend,
                             evidence: str = "(none)") -> list[dict[str, Any]]:
    out = _parse_json(backend(INVARIANT_CLAIM_PROMPT.format(input=render_input(events), evidence=evidence)))
    by_ev = {e["event_id"]: e["text"] for e in events}
    claims = []
    for c in (out.get("claims") or []):
        if not isinstance(c, dict):
            continue
        # keep only sites whose quote verifies; keep the claim if the mechanism quote verifies
        c["sites"] = [s for s in (c.get("sites") or []) if isinstance(s, dict) and _quote_ok(s, by_ev)]
        c["mechanism_verified"] = _quote_ok({"event_id": c.get("event_id"),
                                             "mechanism_quote": c.get("mechanism_quote")}, by_ev)
        claims.append(c)
    return claims


def align_claim_to_gt(claims: list[dict[str, Any]], gt_criterion: dict[str, Any]) -> dict[str, Any]:
    """Layer-1 alignment: canonical object overlap + relation family. No path/line matching.
    Returns the best-matching claim's alignment; `mechanism` is passed through for Layer-2."""
    # GT object tokens: the criterion object/variable PLUS the site vars (aliases of the
    # same physical object, e.g. dc == &sentry->cleanupCallback).
    gt_obj = canon_var(gt_criterion.get("object") or gt_criterion.get("variable"))
    for s in gt_criterion.get("sites") or []:
        gt_obj |= canon_var(s.get("var"))
    gt_rel = canon_relation(gt_criterion.get("relation") or gt_criterion.get("kind"))
    best = {"relation_match": False, "object_match": False, "gt_object": sorted(gt_obj),
            "gt_relation": gt_rel, "agent_object": None, "agent_relation": None,
            "object_overlap": [], "mechanism": None, "n_claims": len(claims)}
    best_score = -1
    for c in claims:
        # agent object tokens: the object field + how it wrote it + the site vars
        agent_toks = canon_var(c.get("object")) | canon_var(c.get("object_raw"))
        for s in c.get("sites") or []:
            agent_toks |= canon_var(s.get("var"))
        ov = agent_toks & gt_obj
        rel_ok = canon_relation(c.get("relation")) == gt_rel
        score = (2 if rel_ok else 0) + (1 if ov else 0)
        if score > best_score:
            best_score = score
            best = {"relation_match": rel_ok, "object_match": bool(ov), "object_overlap": sorted(ov),
                    "agent_object": c.get("object"), "agent_relation": canon_relation(c.get("relation")),
                    "gt_object": sorted(gt_obj), "gt_relation": gt_rel,
                    "mechanism": c.get("mechanism"), "n_claims": len(claims)}
    return best


# --------------------------------------------------------------------------- #
# 6d. Layer-2 mechanism judge + composite understanding score.                  #
#     Layer-1 (object+relation) is cheap but the crash TYPE is given to the      #
#     agent, so relation is nearly free and Layer-1 alone over-credits. The      #
#     understanding score is gated on Layer-2 (the WHY): you cannot exceed 0.4   #
#     without explaining the same root-cause mechanism as the GT.                #
# --------------------------------------------------------------------------- #
def gt_mechanism(criterion: dict[str, Any]) -> str:
    if criterion.get("kind") == "lifetime":
        sites = "; ".join(f"{s.get('role')}@{s.get('function')}:{s.get('line')}(var={s.get('var')})"
                          for s in criterion.get("sites") or [])
        base = f"{criterion.get('relation')} of `{criterion.get('object')}`"
        extra = criterion.get("mechanism") or criterion.get("evidence") or ""
        return f"{base}. sites: {sites}. {str(extra)[:400]}"
    return (f"missing bounds check `{criterion.get('condition')}` on `{criterion.get('variable')}` "
            f"in {criterion.get('region_function')}{criterion.get('region_lines')}. "
            f"{str(criterion.get('note') or '')[:300]}")


MECHANISM_JUDGE_PROMPT = """\
You judge whether a coding agent EXPLAINED THE SAME ROOT-CAUSE MECHANISM as the ground
truth — not merely the same crash symptom. The crash type is GIVEN to the agent, so
naming the bug class is NOT understanding; the mechanism (WHY it happens) is.

GROUND-TRUTH mechanism:
{gt_mechanism}

AGENT's mechanism claim: {agent_mechanism}
AGENT's cited evidence:  {agent_quote}

Output ONLY: {{"verdict":"match|partial|mismatch","why":"<one short sentence>"}}
- match    = same causal reason (same missing check / same aliasing / same lifecycle path).
- partial  = related and on the right object but incomplete or a slightly different cause.
- mismatch = a different cause, or only restates the crash symptom.
Be strict: default to mismatch/partial when unsure."""


def judge_mechanism(agent_mechanism: str, agent_quote: str, gt_criterion: dict[str, Any],
                    backend: LLMBackend) -> dict[str, Any]:
    out = _parse_json(backend(MECHANISM_JUDGE_PROMPT.format(
        gt_mechanism=gt_mechanism(gt_criterion),
        agent_mechanism=agent_mechanism or "(none)", agent_quote=agent_quote or "(none)")))
    v = str(out.get("verdict", "mismatch")).lower()
    if v not in ("match", "partial", "mismatch"):
        v = "mismatch"
    return {"verdict": v, "why": out.get("why", "")}


_MECH_SCORE = {"match": 1.0, "partial": 0.7, "mismatch": 0.4}


def understanding_score(claims: list[dict[str, Any]], gt_criterion: dict[str, Any],
                        backend: LLMBackend | None = None) -> dict[str, Any]:
    """Composite score. relation(given)→object→MECHANISM. Ceiling 0.4 without a
    mechanism match; only the Layer-2 judge lifts it to 0.7/1.0."""
    al = align_claim_to_gt(claims, gt_criterion)
    result = {"score": 0.0, "band": "no_bug_class", "relation_match": al["relation_match"],
              "object_match": al["object_match"], "object_overlap": al["object_overlap"],
              "mechanism_verdict": None, "mechanism_why": None, "agent_mechanism": al.get("mechanism")}
    if not al["relation_match"]:
        return result
    if not al["object_match"]:
        result.update(score=0.2, band="right_class_wrong_object")
        return result
    # Layer-1 satisfied → the WHY decides. Without a backend we can't judge → cap at 0.4.
    if backend is None or not al.get("mechanism"):
        result.update(score=0.4, band="right_what_mechanism_unjudged")
        return result
    # pick the cited quote of the best-aligned claim for grounding
    quote = ""
    for c in claims:
        if c.get("mechanism") == al.get("mechanism"):
            quote = c.get("mechanism_quote") or ""
            break
    j = judge_mechanism(al["mechanism"], quote, gt_criterion, backend)
    result.update(score=_MECH_SCORE[j["verdict"]],
                  band={"match": "understood", "partial": "partial", "mismatch": "right_what_wrong_why"}[j["verdict"]],
                  mechanism_verdict=j["verdict"], mechanism_why=j["why"])
    return result


def _edge_ops(e: dict[str, Any]) -> set[str]:
    """The variables an edge is about — from/to/via/relation/obj, canonicalized."""
    ops: set[str] = set()
    for k in ("from", "to", "via", "relation", "obj"):
        ops |= canon_var(e.get(k))
    return ops


def _edge_type(e: dict[str, Any]) -> str:
    t = str(e.get("type") or "").lower()
    return t if t in ("data", "control", "order") else "other"


def propagation_score(agent_trace: dict[str, Any], gt_verified: dict[str, Any]) -> dict[str, Any]:
    """Layer 3 (HOW) — the source->sink reasoning: did the agent capture the GT's typed
    relationships between variables (data/control/order edges) and the between-nodes?
    Matched SEMANTICALLY: same edge-type family + canonical variable-token overlap. NO
    function-name / line-number / string matching."""
    gt_edges = [e for e in (gt_verified.get("edges") or []) if isinstance(e, dict)]
    ag_edges = [e for e in (agent_trace.get("edges") or []) if isinstance(e, dict)]
    by_type: dict[str, dict[str, int]] = {}
    matched_edges = []
    for ge in gt_edges:
        t = _edge_type(ge)
        slot = by_type.setdefault(t, {"total": 0, "matched": 0})
        slot["total"] += 1
        gops = _edge_ops(ge)
        hit = any(_edge_type(ae) == t and (_edge_ops(ae) & gops) for ae in ag_edges) if gops else False
        slot["matched"] += 1 if hit else 0
        matched_edges.append({"type": t, "via": ge.get("via") or ge.get("relation"),
                              "ops": sorted(gops), "matched": hit})
    e_tot = sum(v["total"] for v in by_type.values())
    e_hit = sum(v["matched"] for v in by_type.values())
    # between-node coverage (the necessary checkpoints that are NOT the source/sink endpoints),
    # matched by canonical VARIABLE overlap — not line.
    gt_nodes = [n for n in (gt_verified.get("nodes") or []) if isinstance(n, dict)
                and str(n.get("role") or "").lower() not in ("source", "sink")]
    ag_nodes = [n for n in (agent_trace.get("nodes") or []) if isinstance(n, dict)]
    n_tot = n_hit = 0
    for gn in gt_nodes:
        gv = canon_var(gn.get("var"))
        if not gv:
            continue
        n_tot += 1
        if any(canon_var(an.get("var")) & gv for an in ag_nodes):
            n_hit += 1
    edge_recall = round(e_hit / e_tot, 3) if e_tot else None
    node_recall = round(n_hit / n_tot, 3) if n_tot else None
    parts = [x for x in (edge_recall, node_recall) if x is not None]
    layer3 = round(sum(parts) / len(parts), 3) if parts else None
    return {"layer3_score": layer3, "edge_recall": edge_recall, "node_recall": node_recall,
            "edge_recall_by_type": {t: round(v["matched"] / v["total"], 3)
                                    for t, v in by_type.items() if v["total"]},
            "gt_edges": e_tot, "agent_edges": len(ag_edges),
            "gt_between_nodes": n_tot, "matched_edges": matched_edges}


def three_layer_score(claims: list[dict[str, Any]], agent_trace: dict[str, Any],
                      gt_criterion: dict[str, Any], gt_verified: dict[str, Any],
                      backend: LLMBackend | None = None) -> dict[str, Any]:
    """Compose the reasoning score across the three layers the GT emphasizes:
      L1 what  = root_cause object + relation
      L2 why   = root_cause mechanism (semantic judge)
      L3 how   = source->sink typed-edge propagation + between-nodes
    Composite weights root-cause understanding (L1+L2) and propagation (L3)."""
    u = understanding_score(claims, gt_criterion, backend)     # L1 + L2
    p = propagation_score(agent_trace, gt_verified)            # L3
    rc = u["score"]
    prop = p["layer3_score"]
    if rc is None:
        composite = None
    elif prop is None:
        composite = rc
    else:
        composite = round(0.6 * rc + 0.4 * prop, 3)
    return {
        "layer1_what": {"relation_match": u["relation_match"], "object_match": u["object_match"],
                        "object_overlap": u["object_overlap"]},
        "layer2_why": {"verdict": u.get("mechanism_verdict"), "why": u.get("mechanism_why")},
        "layer3_how": {"score": prop, "edge_recall": p["edge_recall"],
                       "edge_recall_by_type": p["edge_recall_by_type"], "node_recall": p["node_recall"]},
        "root_cause_understanding": rc, "propagation_reasoning": prop, "composite": composite,
    }


def aggregate_claims(events: list[dict[str, Any]], backend: LLMBackend,
                     evidence: str = "(none)", k: int = 3) -> list[dict[str, Any]]:
    """Run extraction k times and union the claims — align/score then picks the best,
    so this raises recall and damps single-run variance (reported via `k`)."""
    out: list[dict[str, Any]] = []
    for _ in range(max(1, k)):
        out.extend(extract_invariant_claims(events, backend, evidence))
    return out


def score_understanding_from_trajectory(trajectory: list, gt_criterion: dict[str, Any],
                                        backend: LLMBackend, k: int = 3) -> dict[str, Any]:
    """End-to-end: extract (k-run aggregated) invariant claims from a trajectory and
    score understanding against the GT criterion. GT-blind extraction, GT used only here."""
    events = build_observer_input(trajectory)
    bank = build_evidence_bank(trajectory)
    claims = aggregate_claims(events, backend, evidence_digest(bank), k=k)
    res = understanding_score(claims, gt_criterion, backend)
    res["k_runs"] = k
    res["n_claims"] = len(claims)
    return res


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
