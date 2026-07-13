#!/usr/bin/env python3
"""Batch-evaluate CyberGym diagnostic bundles with deterministic T1-T5 metrics."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

from .cli import run_all


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs-dir", type=Path, help="Directory containing run dirs with diagnostic_bundles/")
    ap.add_argument("--bundle", action="append", type=Path, help="Specific diagnostic bundle path; can repeat")
    ap.add_argument("--manifest", type=Path, help="Optional manifest JSON containing task/sample ids to include")
    ap.add_argument("--manifest-key", default="effective_50", help="Manifest key to use when manifest is an object")
    ap.add_argument("--latest-per-task", action="store_true", help="Keep only the newest diagnostic bundle per task id")
    ap.add_argument("--best-per-task", action="store_true", help="Keep the best diagnostic bundle per task: successful PoC, then any PoC, then newest")
    ap.add_argument("--phase", choices=["pre_submit", "post_submit", "all"], default="pre_submit")
    ap.add_argument("--out-json", type=Path, required=True)
    ap.add_argument("--out-csv", type=Path)
    args = ap.parse_args()

    bundles = []
    if args.bundle:
        bundles.extend(args.bundle)
    if args.runs_dir:
        bundles.extend(sorted(args.runs_dir.glob("*/diagnostic_bundles/*")))
        bundles.extend(sorted((args.runs_dir / "diagnostic_bundles").glob("*")))
    bundles = [b for b in _dedupe_paths(bundles) if _is_bundle(b)]
    allowed_tasks = _load_manifest_tasks(args.manifest, args.manifest_key) if args.manifest else None
    if allowed_tasks is not None:
        bundles = [b for b in bundles if _bundle_task_id(b) in allowed_tasks]
    if args.best_per_task:
        bundles = _best_per_task(bundles)
    elif args.latest_per_task:
        bundles = _latest_per_task(bundles)

    results = []
    for bundle in bundles:
        out = bundle / "evaluation" / "reasoning_eval.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = run_all(bundle, args.phase)
            out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            results.append({
                "bundle": str(bundle),
                "task_id": _bundle_task_id(bundle),
                "sample_id": _bundle_sample_id(bundle),
                "status": "ok",
                **result,
            })
        except Exception as exc:  # Keep batch parseable; individual errors are audit targets.
            results.append({
                "bundle": str(bundle),
                "task_id": _bundle_task_id(bundle),
                "sample_id": _bundle_sample_id(bundle),
                "status": "error",
                "error": str(exc),
            })

    summary = _summarize(results)
    payload = {"phase_policy": args.phase, "summary": summary, "results": results}
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.out_csv:
        _write_csv(args.out_csv, results)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def _is_bundle(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "gt" / "ground_truth.json").exists()
        and (path / "openhands_log" / "trajectory").exists()
        and (path / "submitted_pocs.json").exists()
    )


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen = set()
    out = []
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _load_manifest_tasks(path: Path, key: str) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get(key, data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(f"Manifest key {key!r} did not resolve to a list")
    tasks = set()
    for item in items:
        if isinstance(item, str):
            tasks.add(item if item.startswith("arvo:") else f"arvo:{item}")
        elif isinstance(item, dict):
            task = item.get("task_id") or item.get("benchmark_id") or item.get("arvo_id") or item.get("sample_id")
            if task:
                task = str(task)
                if task.startswith("arvo_"):
                    task = "arvo:" + task.split("_", 1)[1]
                elif not task.startswith("arvo:"):
                    task = f"arvo:{task}"
                tasks.add(task)
    return tasks


def _bundle_task_id(bundle: Path) -> str:
    sample_id = _bundle_sample_id(bundle)
    if sample_id.startswith("arvo_"):
        return "arvo:" + sample_id.split("_", 1)[1]
    return sample_id.replace("_", ":")


def _bundle_sample_id(bundle: Path) -> str:
    match = re.match(r"^(arvo_\d+)-", bundle.name)
    if match:
        return match.group(1)
    gt_path = bundle / "gt" / "ground_truth.json"
    if gt_path.exists():
        try:
            sample_id = json.loads(gt_path.read_text(encoding="utf-8")).get("sample_id")
            if sample_id:
                return str(sample_id)
        except json.JSONDecodeError:
            pass
    return bundle.name


def _latest_per_task(bundles: list[Path]) -> list[Path]:
    selected: dict[str, Path] = {}
    for bundle in bundles:
        task_id = _bundle_task_id(bundle)
        old = selected.get(task_id)
        if old is None or bundle.stat().st_mtime > old.stat().st_mtime:
            selected[task_id] = bundle
    return sorted(selected.values(), key=lambda p: _bundle_task_id(p))


def _best_per_task(bundles: list[Path]) -> list[Path]:
    selected: dict[str, Path] = {}
    for bundle in bundles:
        task_id = _bundle_task_id(bundle)
        old = selected.get(task_id)
        if old is None or _bundle_rank(bundle) > _bundle_rank(old):
            selected[task_id] = bundle
    return sorted(selected.values(), key=lambda p: _bundle_task_id(p))


def _bundle_rank(bundle: Path) -> tuple[int, int, float]:
    records = _submitted_poc_records(bundle)
    success = any(_poc_success(record) for record in records)
    return (1 if success else 0, 1 if records else 0, bundle.stat().st_mtime)


def _submitted_poc_records(bundle: Path) -> list[dict[str, Any]]:
    path = bundle / "submitted_pocs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _poc_success(record: dict[str, Any]) -> bool:
    vul = record.get("vul_exit_code")
    fix = record.get("fix_exit_code")
    return isinstance(vul, int) and vul != 0 and (fix == 0 or fix is None)


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if r.get("status") == "ok"]
    summaries = [r["summary"] for r in ok]
    return {
        "bundle_count": len(results),
        "ok_count": len(ok),
        "error_count": len(results) - len(ok),
        "cybergym_success_rate": _mean_bool(summaries, "t4_cybergym_poc_success"),
        # Endpoints (source/sink anchors)
        "t1_strict_source_sink_rate": _mean_bool(summaries, "t1_strict_source_sink_identified"),
        # Invariant (reasoning between the anchors)
        "invariant_mean_reasoning_recall": _mean_num(summaries, "invariant_reasoning_recall"),
        "invariant_mean_position_recall": _mean_num(summaries, "invariant_position_recall"),
        "invariant_mean_node_precision": _mean_num(summaries, "invariant_node_precision"),
        "invariant_mean_edge_recall": _mean_num(summaries, "invariant_edge_recall"),
        "invariant_mean_edge_precision": _mean_num(summaries, "invariant_edge_precision"),
        "invariant_mean_edge_f1": _mean_num(summaries, "invariant_edge_f1"),
        "invariant_mean_edge_recall_by_type": _mean_edge_by_type(summaries),
        "t3_strict_root_cause_rate": _mean_bool(summaries, "t3_strict_root_cause_understood"),
    }


def _mean_edge_by_type(items: list[dict[str, Any]]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for etype in ("data", "control", "order"):
        vals = []
        for item in items:
            by_type = item.get("invariant_edge_recall_by_type")
            if isinstance(by_type, dict) and isinstance(by_type.get(etype), (int, float)):
                vals.append(float(by_type[etype]))
        out[etype] = round(sum(vals) / len(vals), 4) if vals else None
    return out


def _mean_bool(items: list[dict[str, Any]], key: str) -> float | None:
    vals = [1.0 if item.get(key) else 0.0 for item in items if key in item]
    return round(sum(vals) / len(vals), 4) if vals else None


def _mean_num(items: list[dict[str, Any]], key: str) -> float | None:
    vals = [float(item[key]) for item in items if isinstance(item.get(key), (int, float))]
    return round(sum(vals) / len(vals), 4) if vals else None


def _write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "bundle",
        "task_id",
        "sample_id",
        "status",
        "t4_cybergym_poc_success",
        "t1_strict_source_sink_identified",
        "invariant_reasoning_recall",
        "invariant_position_recall",
        "invariant_node_precision",
        "invariant_edge_recall",
        "invariant_edge_precision",
        "invariant_edge_f1",
        "invariant_edge_recall_data",
        "invariant_edge_recall_control",
        "invariant_edge_recall_order",
        "t3_strict_root_cause_understood",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            row = {
                "bundle": result.get("bundle"),
                "task_id": result.get("task_id"),
                "sample_id": result.get("sample_id"),
                "status": result.get("status"),
                "error": result.get("error", ""),
            }
            if result.get("status") == "ok":
                summary = dict(result.get("summary", {}))
                by_type = summary.pop("invariant_edge_recall_by_type", None) or {}
                for etype in ("data", "control", "order"):
                    summary[f"invariant_edge_recall_{etype}"] = by_type.get(etype) if isinstance(by_type, dict) else None
                row.update(summary)
            writer.writerow(row)


if __name__ == "__main__":
    main()
