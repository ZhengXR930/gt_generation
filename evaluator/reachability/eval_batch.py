#!/usr/bin/env python3
"""Format failure-localization for subject PoCs, in batch (coverage-based).

For every generated sample under poc_generation/poc_results/, locate the PoC the
subject submitted (the CyberGym server persists its bytes + the vulnerable-run
sanitizer output), run the R1-R5 reachability evaluation against the sample's
ground_truth, and record WHERE the PoC fell short along the input -> sink chain:

    parser_not_admitted -> source_not_reached -> vulnerable_function_not_reached
    -> vulnerable_line_not_reached -> line_reached_but_not_triggered -> triggered

Reachability signal is libFuzzer SanitizerCoverage (reachability/coverage.py):
run the target once with -print_coverage=1 and read which functions/lines the
PoC reached -- no debugger, no ptrace, so it works under qemu emulation where
gdb cannot. R5 comes from the saved vulnerable-run sanitizer output. Scoring is
the existing evaluate_r1_r5(); this driver just feeds it coverage-derived hits.

Writes poc_results/<id>/reachability_eval.json per sample.

Run as a module (evaluator/ on PYTHONPATH):
    PYTHONPATH=evaluator python3 -m reachability.eval_batch
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from reachability.core import evaluate_r1_r5
from reachability.engine import extract_reachability_checkpoints
from reachability.coverage import coverage_hits

REPO_ROOT = Path(__file__).resolve().parents[2]
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"
SERVER_LOGS = REPO_ROOT / "poc_generation" / "poc_generator" / "server" / "logs"
GT_RESULTS = REPO_ROOT / "gt_results"


def _poc_id(manifest: dict[str, Any]) -> str | None:
    pg = manifest.get("poc_generation") or {}
    if pg.get("poc_id"):
        return str(pg["poc_id"])
    subs = pg.get("submissions") or []
    if subs and isinstance(subs[0], dict) and subs[0].get("poc_id"):
        return str(subs[0]["poc_id"])
    return None


def _server_paths(poc_id: str) -> tuple[Path, Path]:
    """CyberGym stores each submission at logs/<id[:2]>/<id[2:4]>/<id>/."""
    d = SERVER_LOGS / poc_id[:2] / poc_id[2:4] / poc_id
    return d / "poc.bin", d / "output.vul"


def _arvo_image(sample_id: str, manifest: dict[str, Any]) -> str | None:
    """The vulnerable ARVO image for an arvo_<id> sample; None for other tracks."""
    if manifest.get("arvo_id"):
        return f"n132/arvo:{manifest['arvo_id']}-vul"
    if sample_id.startswith("arvo_"):
        return f"n132/arvo:{sample_id.removeprefix('arvo_')}-vul"
    return None


def evaluate_sample(sample_id: str, timeout: int) -> dict[str, Any]:
    gt_path = GT_RESULTS / sample_id / "ground_truth.json"
    manifest_path = POC_RESULTS / sample_id / "manifest.json"
    if not gt_path.is_file():
        return {"sample_id": sample_id, "skipped": "no ground_truth"}
    if not manifest_path.is_file():
        return {"sample_id": sample_id, "skipped": "no manifest"}

    manifest = json.loads(manifest_path.read_text())
    poc_id = _poc_id(manifest)
    if not poc_id:
        return {"sample_id": sample_id, "skipped": "no submitted PoC in manifest"}
    poc_bin, output_vul = _server_paths(poc_id)
    if not poc_bin.is_file():
        return {"sample_id": sample_id, "skipped": f"PoC bytes missing: {poc_bin.name}"}

    image = _arvo_image(sample_id, manifest)
    if image is None:
        # repo/secbench track: the target is built in gt-memory-env, not a
        # prebuilt image -- coverage there needs the built binary (a later step).
        return {"sample_id": sample_id, "skipped": "non-ARVO track (repo binary not built here)"}

    gt = json.loads(gt_path.read_text())
    checkpoints = extract_reachability_checkpoints(gt)
    try:
        hits = coverage_hits(image=image, poc_path=poc_bin, checkpoints=checkpoints, timeout=timeout)
    except Exception as exc:
        return {"sample_id": sample_id, "error": f"{type(exc).__name__}: {exc}"}

    sanitizer_trace = output_vul.read_text(errors="replace") if output_vul.is_file() else None
    report = evaluate_r1_r5(gt=gt, hits=hits, sanitizer_trace=sanitizer_trace, checkpoints=checkpoints)
    (POC_RESULTS / sample_id / "reachability_eval.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {
        "sample_id": sample_id,
        "failure_stage": report.get("failure_stage"),
        "R1": report.get("R1_parser_admitted"),
        "R2": report.get("R2_source_reached"),
        "R3": report.get("R3_vulnerable_function_reached"),
        "R4": report.get("R4_vulnerable_line_reached"),
        "R5": report.get("R5_sanitizer_triggered"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample-id", action="append",
                    help="Evaluate only this sample (repeatable); default is every poc_results sample.")
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args(argv)

    if args.sample_id:
        samples = list(dict.fromkeys(args.sample_id))
    else:
        samples = sorted(p.name for p in POC_RESULTS.iterdir()
                         if p.is_dir() and (p / "manifest.json").is_file())

    def m(v: Any) -> str:
        return "T" if v is True else ("F" if v is False else "-")

    print(f"{'sample':16s} {'failure_stage':34s} R1 R2 R3 R4 R5")
    print("-" * 80)
    rows = []
    for s in samples:
        r = evaluate_sample(s, args.timeout)
        rows.append(r)
        if "skipped" in r:
            print(f"{s:16s} (skipped: {r['skipped']})")
        elif "error" in r:
            print(f"{s:16s} ERROR: {r['error'][:48]}")
        else:
            print(f"{s:16s} {str(r['failure_stage']):34s} "
                  f"{m(r['R1'])}  {m(r['R2'])}  {m(r['R3'])}  {m(r['R4'])}  {m(r['R5'])}")
    out = POC_RESULTS / "reachability_eval_report.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
