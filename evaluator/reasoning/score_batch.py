#!/usr/bin/env python3
"""Score every collected reasoning trace against its sample's verified
invariants (two-tier: deterministic function/identifier gate + grounded LLM
judge) and print a consolidated table + write a JSON report.

Reads poc_generation/poc_results/<sample_id>/reasoning_trace.json (produced by
collect_trace.py) and writes evaluator/reasoning/reasoning_eval_report.json.

Run as a module so the package-relative import resolves:
    python3 -m evaluator.reasoning.score_batch
"""
import json
from pathlib import Path

from .scoring import score_trace

REPO_ROOT = Path(__file__).resolve().parents[2]
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"
OUT = Path(__file__).resolve().parent / "reasoning_eval_report.json"


def main() -> None:
    traces = sorted(POC_RESULTS.glob("*/reasoning_trace.json"))
    rows = []
    for path in traces:
        sid = path.parent.name
        stored = json.loads(path.read_text())
        resp = stored.get("response", "")
        trigger = stored.get("trigger")
        try:
            r = score_trace(sid, resp, use_judge=True)
        except Exception as exc:
            rows.append({"sample_id": sid, "error": f"{type(exc).__name__}: {exc}", "trigger": trigger})
            continue
        r["trigger"] = trigger
        rows.append(r)

    print(f"{'sample':16s} {'trig':14s} {'parsed':6s} {'score':6s} {'cap/tot':8s}  captured-invariants")
    print("-" * 100)
    scored = []
    for r in rows:
        if "error" in r:
            print(f"{r['sample_id']:16s} {str(r.get('trigger'))[:14]:14s}  ERROR: {r['error']}")
            continue
        caps = [it["id"].replace("prop:", "") for it in r["items"] if it["captured"]]
        score = r["score"]
        scored.append(score if score is not None else 0.0)
        score_s = "n/a" if score is None else f"{score:.2f}"
        print(f"{r['sample_id']:16s} {str(r.get('trigger'))[:14]:14s} {str(r['trace_parsed']):6s} {score_s:6s} "
              f"{r['captured']}/{r['n_invariants']:<6d}  {', '.join(caps) if caps else '(none)'}")

    print("-" * 100)
    if scored:
        mean = sum(scored) / len(scored)
        full = sum(1 for s in scored if s == 1.0)
        zero = sum(1 for s in scored if s == 0.0)
        print(f"samples scored: {len(scored)}   mean capture rate: {mean:.2f}   "
              f"perfect(1.0): {full}   zero(0.0): {zero}")
    OUT.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
