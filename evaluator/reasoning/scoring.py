#!/usr/bin/env python3
"""Score a subject's free-form vulnerability logic-chain (trace) against a
sample's VERIFIED invariants, two-tier:

  Tier 1 (deterministic, always): for each invariant, require the trace to
    (a) visit the invariant's function(s) [function-overlap, the reliable
    gate], and (b) mention at least one DISTINCTIVE identifier of the
    invariant's operand(s). Cheap, auditable, reproducible, no LLM. Kills
    wrong-code-path confabulations here (they never overlap the functions).

  Tier 2 (grounded LLM judge, only on invariants that pass Tier 1): confirm
    the trace states the invariant's RELATION/ROLE correctly -- catches
    "mentions the right variable but says the wrong thing about it" and
    credits a correct relation expressed in prose ("unchanged" for "=="),
    which Tier 1's literal-operator check cannot. The judge must quote a
    verbatim substring of the trace; a non-substring quote => not confirmed.

An invariant is CAPTURED iff it passes Tier 1 AND (Tier-2 judge confirms, or
the judge is disabled). Score = captured / total verified invariants.

The invariant IS the grading key; there are no probe questions. See the
session this was built from for why this replaced generated questions.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]   # evaluator/reasoning/ -> repo root
GT_RESULTS = REPO_ROOT / "gt_results"

from .gt_invariants import (  # noqa: E402
    load, load_field_bindings, load_event_locations,
    edge_for_assertion, operand_name,
)

_OP_SYMBOL = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
# Generic identifier segments that are NOT distinctive enough to prove the
# trace is about the right quantity (they show up in unrelated code too, so
# Tier-1's identifier check must ignore them and lean on the distinctive ones).
_GENERIC_IDS = {
    "info", "ctx", "data", "buf", "len", "ret", "val", "ptr", "idx", "tmp",
    "obj", "size", "count", "num", "var", "value", "index", "result", "arg",
}


def _identifier_tokens(expr: Any) -> set[str]:
    """Identifier segments of an operand/variable expression, split on C
    punctuation. 'variable.n_missing_values' -> {'variable','n_missing_values'};
    '*crl' -> {'crl'}; 'info->label' -> {'info','label'}."""
    return {t for t in re.split(r"[.\[\]()\->*&,\s]+", str(expr or "")) if len(t) >= 3}


def _distinctive(tokens: set[str]) -> set[str]:
    return {t for t in tokens if t.lower() not in _GENERIC_IDS}


def build_invariant_checklist(sample_id: str) -> list[dict[str, Any]]:
    """The verified facts to look for, each with the deterministic features
    (functions, distinctive identifier tokens, operator) and the human-readable
    verified statement (for the Tier-2 judge)."""
    d = GT_RESULTS / sample_id
    gt = load(d, "ground_truth.json")
    vi = load(d, "verified_invariants.json")
    va = load(d, "verified_assertions.json")
    fb = load_field_bindings(d)
    el = load_event_locations(d)
    edges = {
        str(i.get("invariant_id")): i
        for i in vi.get("edges", [])
        if isinstance(i, dict) and i.get("invariant_id")
    }
    checklist: list[dict[str, Any]] = []

    # source
    src = gt.get("source") or {}
    svar = src.get("var") or next(
        (s.get("var") for s in gt.get("fine_trace", [])
         if s.get("line") == src.get("line") and s.get("var")), None
    )
    if svar:
        toks = _identifier_tokens(svar)
        checklist.append({
            "id": "source", "kind": "source",
            "functions": {src.get("function")} - {None},
            "id_tokens": toks, "operator": None,
            "fact": f"The attacker-influenced value first enters via `{svar}` in {src.get('function')}.",
        })

    # mechanism / root cause
    rcc = vi.get("root_cause_criterion")
    if isinstance(rcc, dict) and (rcc.get("variable") or rcc.get("description")):
        toks = _identifier_tokens(rcc.get("variable"))
        checklist.append({
            "id": "root_cause", "kind": "mechanism",
            "functions": {rcc.get("function")} - {None},
            "id_tokens": toks, "operator": None,
            "fact": f"Root cause / required safety condition: {rcc.get('description') or rcc.get('variable')}",
        })

    # propagation edges (verified transition assertions)
    for a in va.get("assertions", []):
        if not isinstance(a, dict) or a.get("kind") != "transition":
            continue
        eid = edge_for_assertion(a, set(edges))
        if not eid:
            continue
        edge = edges[eid]
        lt = operand_name(a["check"][1], a, edge, fb)
        rt = operand_name(a["check"][2], a, edge, fb)
        toks = _identifier_tokens(lt) | _identifier_tokens(rt)
        funcs = set()
        for ev in (a.get("from"), a.get("at")):
            loc = el.get(ev, {})
            if loc.get("function"):
                funcs.add(loc["function"])
        note = edge.get("relation", "")
        checklist.append({
            "id": f"prop:{eid}", "kind": "propagation",
            "functions": funcs, "id_tokens": toks,
            "operator": _OP_SYMBOL.get(a["check"][0], a["check"][0]),
            "fact": f"Propagation: {note}" if note else f"Propagation relation: {lt} {_OP_SYMBOL.get(a['check'][0])} {rt}",
        })
    return checklist


def parse_trace(response: str) -> tuple[set[str], str, list[dict]]:
    """Extract (function-set, searchable-text-blob, steps) from the subject's
    JSON-array trace response (tolerating ``` fences / wrappers)."""
    body = (response or "").strip()
    if "```" in body:
        for chunk in body.split("```"):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("["):
                body = c
                break
    steps: list[dict] = []
    try:
        parsed = json.loads(body)
        if isinstance(parsed, list):
            steps = [s for s in parsed if isinstance(s, dict)]
    except Exception:
        steps = []
    if steps:
        funcs, blob = set(), []
        for s in steps:
            funcs.add(str(s.get("function") or ""))
            blob.append(" ".join(str(s.get(k, "")) for k in ("function", "code", "value_effect", "description")))
        return {f for f in funcs if f}, "  ".join(blob), steps
    # Strict JSON failed (models sometimes emit invalid JSON -- e.g. a bare
    # range "line": 27-42, or an unescaped quote inside code). The scorer only
    # needs function names + the code/value_effect text, never `line`, so
    # regex-extract those string fields per object instead of dropping the
    # sample. steps stays [] to flag that strict parsing did not succeed.
    funcs = set(re.findall(r'"function"\s*:\s*"([^"]*)"', body))
    blob_parts = re.findall(r'"(?:function|code|value_effect|description)"\s*:\s*"([^"]*)"', body)
    return {f for f in funcs if f}, "  ".join(blob_parts), steps


def tier1_deterministic(inv: dict[str, Any], trace_funcs: set[str], trace_blob: str) -> dict[str, Any]:
    func_hit = bool(inv["functions"] & trace_funcs) if inv["functions"] else False
    distinctive = _distinctive(inv["id_tokens"])
    # Fall back to all tokens only if there are no distinctive ones at all.
    probe_toks = distinctive or inv["id_tokens"]
    id_hit = any(re.search(rf"\b{re.escape(t)}\b", trace_blob) for t in probe_toks)
    passed = func_hit and id_hit
    return {"func_hit": func_hit, "id_hit": id_hit, "distinctive": sorted(distinctive), "tier1_pass": passed}


def _judge_once(fact: str, trace_blob: str) -> dict[str, Any]:
    from .judge_llm import client, JUDGE_MODEL  # lazy: only when judging

    system = (
        "You check whether a subject's vulnerability trace demonstrates understanding of ONE verified fact "
        "about the bug -- the same variable/field, the same relation/operation, the same role in the bug. "
        "Paraphrase and different line numbers are fine.\n"
        "IMPORTANT: a verified fact may be phrased as a requirement ('X must be set to NULL', 'the fix adds "
        "X'). A subject explaining the BUG will often correctly describe the SAME fact as its violation "
        "('X is missing', 'X is never done', 'the check is absent'). Both capture the fact -- mark "
        "present=true whether the subject states the requirement OR correctly identifies it as the "
        "missing/violated thing that causes the bug. Only mark present=false if the trace concerns a "
        "DIFFERENT variable/mechanism/role, however confident it sounds.\n"
        "Quote verbatim the exact substring of the SUBJECT TRACE you rely on; if nothing in the trace "
        'concerns this fact, quoted must be "" and present=false. Return ONLY JSON: '
        '{"quoted": "<verbatim trace substring or empty>", "present": true/false, "why": "<one sentence>"}'
    )
    user = f"Verified fact:\n{fact}\n\nSubject trace:\n{trace_blob}"
    resp = client().chat.completions.create(
        model=JUDGE_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=250, timeout=90, temperature=0,
    )
    text = (resp.choices[0].message.content or "").strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        r = json.loads(text.strip())
    except Exception as exc:
        return {"present": False, "why": f"judge parse fail: {exc}", "quote_real": False}
    # Grounding by TOKEN OVERLAP, not exact substring: the judge's quote must
    # be genuinely drawn from the trace (blocks fabricated support), but an
    # exact whitespace-normalized substring test was too brittle -- the judge
    # lightly reformats its quote run to run (and DeepSeek is not bit-
    # deterministic even at temperature 0), so a valid quote would randomly
    # fail. Require most of the quote's content tokens to appear in the blob.
    quote = str(r.get("quoted") or "")
    qtoks = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", quote) if len(t) >= 3]
    blob_toks = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", trace_blob))
    overlap = sum(1 for t in qtoks if t in blob_toks) / len(qtoks) if qtoks else 0.0
    quote_real = overlap >= 0.6
    return {"present": bool(r.get("present")) and quote_real, "quote_real": quote_real, "why": r.get("why")}


def _judge(fact: str, trace_blob: str, votes: int = 3) -> dict[str, Any]:
    """Majority vote over independent judge calls to damp residual
    nondeterminism (DeepSeek is not bit-deterministic even at temperature 0)."""
    results = [_judge_once(fact, trace_blob) for _ in range(votes)]
    present_votes = sum(1 for r in results if r["present"])
    decided = present_votes * 2 > votes
    # Report the reasoning from a vote that matches the majority verdict.
    why = next((r["why"] for r in results if r["present"] == decided), results[0]["why"])
    return {"present": decided, "votes": f"{present_votes}/{votes}", "why": why}


def score_trace(sample_id: str, response: str, use_judge: bool = True) -> dict[str, Any]:
    checklist = build_invariant_checklist(sample_id)
    trace_funcs, trace_blob, steps = parse_trace(response)
    items = []
    captured = 0
    for inv in checklist:
        t1 = tier1_deterministic(inv, trace_funcs, trace_blob)
        judged = None
        if t1["tier1_pass"] and use_judge:
            judged = _judge(inv["fact"], trace_blob)
            is_captured = bool(judged["present"])
        else:
            # No judge (or failed Tier 1): Tier-1 result stands on its own.
            is_captured = t1["tier1_pass"]
        if is_captured:
            captured += 1
        items.append({
            "id": inv["id"], "kind": inv["kind"], "fact": inv["fact"],
            "tier1": t1, "tier2": judged, "captured": is_captured,
        })
    return {
        "sample_id": sample_id,
        "trace_parsed": bool(steps),
        "trace_functions": sorted(trace_funcs),
        "n_invariants": len(checklist),
        "captured": captured,
        "score": (captured / len(checklist)) if checklist else None,
        "items": items,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-id", required=True)
    ap.add_argument("--trace-file", type=Path, required=True,
                    help="JSON file with the subject's trace response under a 'response' key")
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()
    resp = json.loads(args.trace_file.read_text()).get("response", "")
    result = score_trace(args.sample_id, resp, use_judge=not args.no_judge)
    print(json.dumps(result, indent=2, ensure_ascii=False))
