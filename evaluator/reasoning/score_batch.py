"""Score subject final fine traces against verified invariants.

The main evaluator path is deterministic and does not use an LLM judge. The
optional judge remains available for ablations through --use-judge.

Reads poc_generation/poc_results/<model>/<sample_id>/fine_trace.json and writes
evaluator/reasoning/fine_trace_eval_report.json by default.

Run as a module so the package-relative import resolves:
    python3 -m evaluator.reasoning.score_batch
"""
import argparse
import json
from pathlib import Path

from .scoring import score_trace

REPO_ROOT = Path(__file__).resolve().parents[2]
POC_RESULTS = REPO_ROOT / "poc_generation" / "poc_results"
GT_RESULTS = REPO_ROOT / "gt_results"
OUT = Path(__file__).resolve().parent / "fine_trace_eval_report.json"


def discover_traces(
    *,
    models: list[str] | None = None,
    sample_ids: list[str] | None = None,
) -> list[tuple[str, str, Path]]:
    selected_models = set(models or [])
    selected_samples = set(sample_ids or [])
    rows = []
    for model_dir in sorted(path for path in POC_RESULTS.iterdir() if path.is_dir()):
        if model_dir.name.startswith("_"):
            continue
        if selected_models and model_dir.name not in selected_models:
            continue
        for sample_dir in sorted(path for path in model_dir.iterdir() if path.is_dir()):
            if selected_samples and sample_dir.name not in selected_samples:
                continue
            trace_path = sample_dir / "fine_trace.json"
            if trace_path.is_file():
                rows.append((model_dir.name, sample_dir.name, trace_path))
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append")
    parser.add_argument("--sample-id", action="append")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--use-judge",
        action="store_true",
        help="Enable optional LLM judge ablation. Default is deterministic only.",
    )
    args = parser.parse_args(argv)

    traces = discover_traces(models=args.model, sample_ids=args.sample_id)
    rows = []
    for model, sid, path in traces:
        stored = json.loads(path.read_text())
        resp = json.dumps(stored, ensure_ascii=False)
        manifest_path = path.parent / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text())
            if manifest_path.is_file()
            else {}
        )
        trigger = manifest.get("status")
        if not (GT_RESULTS / sid / "ground_truth.json").is_file():
            rows.append({
                "model": model,
                "sample_id": sid,
                "skipped": "no GT",
                "trigger": trigger,
            })
            continue
        try:
            r = score_trace(sid, resp, use_judge=args.use_judge)
        except (KeyError, TypeError, ValueError, FileNotFoundError) as exc:
            rows.append({
                "model": model,
                "sample_id": sid,
                "error": f"{type(exc).__name__}: {exc}",
                "trigger": trigger,
            })
            continue
        r["model"] = model
        r["trigger"] = trigger
        rows.append(r)

    print(
        f"{'model':18s} {'sample':16s} {'trig':14s} {'parsed':6s} "
        f"{'score':6s} {'cap/tot':8s}  captured-invariants"
    )
    print("-" * 100)
    scored = []
    for r in rows:
        model = str(r.get("model") or "")[:18]
        sid = str(r.get("sample_id") or "")
        if "skipped" in r:
            print(
                f"{model:18s} {sid:16s} {str(r.get('trigger'))[:14]:14s}  "
                f"SKIP: {r['skipped']}"
            )
            continue
        if "error" in r:
            print(
                f"{model:18s} {sid:16s} {str(r.get('trigger'))[:14]:14s}  "
                f"ERROR: {r['error']}"
            )
            continue
        caps = [it["id"].replace("prop:", "") for it in r["items"] if it["captured"]]
        score = r["score"]
        scored.append(score if score is not None else 0.0)
        score_s = "n/a" if score is None else f"{score:.2f}"
        print(
            f"{model:18s} {sid:16s} {str(r.get('trigger'))[:14]:14s} "
            f"{r['trace_parsed']!s:6s} {score_s:6s} "
            f"{r['captured']}/{r['n_invariants']:<6d}  "
            f"{', '.join(caps) if caps else '(none)'}"
        )

    print("-" * 100)
    if scored:
        mean = sum(scored) / len(scored)
        full = sum(1 for s in scored if s == 1.0)
        zero = sum(1 for s in scored if s == 0.0)
        print(f"samples scored: {len(scored)}   mean capture rate: {mean:.2f}   "
              f"perfect(1.0): {full}   zero(0.0): {zero}")
    args.out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
