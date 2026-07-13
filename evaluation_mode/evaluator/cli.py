#!/usr/bin/env python3
"""Run the deterministic reasoning evaluators against the agent's recorded node-trace.

  * t1        — endpoints: source/sink localization.
  * invariant — the reasoning between the anchors: between-node invariants + typed
                data/control/order edges.
  * t3        — root cause: the cause-vs-symptom distinction and cause->crash link.
  * t4        — dynamic PoC-trigger success.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .base import EvaluationInput
from .invariant import InvariantEvaluator
from .poc_attempt_binder import write_bound_poc_attempts
from .t1_source_sink import T1SourceSinkEvaluator
from .t3_root_cause import T3RootCauseEvaluator
from .t4_poc_success import T4PocSuccessEvaluator


def run_all(bundle: Path, phase: str) -> dict:
    gt = bundle / "gt" / "ground_truth.json"
    trajectory = bundle / "openhands_log" / "trajectory"
    if not trajectory.exists():
        trajectory = bundle / "trajectory"
    submitted = bundle / "submitted_pocs.json"
    if not submitted.exists() and trajectory.exists():
        write_bound_poc_attempts(trajectory, submitted)
    inputs = EvaluationInput(ground_truth=gt, trajectory=trajectory, submitted_pocs=submitted)
    metrics = {
        "t1": T1SourceSinkEvaluator().evaluate(inputs),
        "invariant": InvariantEvaluator().evaluate(inputs),
        "t3": T3RootCauseEvaluator().evaluate(inputs),
        "t4": T4PocSuccessEvaluator().evaluate(inputs),
    }
    structured = all(bool(metrics[k].get("structured_reasoning_evaluable")) for k in ("t1", "invariant", "t3"))
    inv = metrics["invariant"]["summary"]
    return {
        "bundle": str(bundle),
        "phase_policy": phase,
        "metrics": metrics,
        "summary": {
            "structured_reasoning_evaluable": structured,
            # Endpoints (source/sink anchors)
            "t1_strict_source_sink_identified": metrics["t1"]["summary"]["strict_source_sink_identified"],
            # Invariant (reasoning between the anchors)
            "invariant_reasoning_recall": inv["reasoning_recall"],
            "invariant_position_recall": inv["position_recall"],
            "invariant_node_precision": inv["node_precision"],
            "invariant_edge_recall": inv["edge_recall"],
            "invariant_edge_precision": inv["edge_precision"],
            "invariant_edge_f1": inv["edge_f1"],
            "invariant_edge_recall_by_type": inv["edge_recall_by_type"],
            "invariant_reasoning_points": inv["reasoning_points"],
            # Root cause
            "t3_strict_root_cause_understood": metrics["t3"]["summary"]["strict_root_cause_understood"],
            # Dynamic
            "t4_cybergym_poc_success": metrics["t4"]["summary"]["cybergym_poc_success"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="metric", required=True)

    for metric in ["invariant", "t1", "t3"]:
        p = sub.add_parser(metric, help=f"Evaluate {metric}")
        p.add_argument("--gt", required=True, type=Path, help="Path to ground_truth.json")
        p.add_argument("--trajectory", required=True, type=Path, help="Path to the recorder trajectory")
        p.add_argument("--output", type=Path, help="Output JSON path")

    t4 = sub.add_parser("t4", help="Evaluate CyberGym PoC success")
    t4.add_argument("--submitted-pocs", required=True, type=Path, help="Path to submitted_pocs.json")
    t4.add_argument("--output", type=Path, help="Output JSON path")

    bind = sub.add_parser("bind-pocs", help="Bind submit.sh attempts to pre-submit reasoning state")
    bind.add_argument("--trajectory", required=True, type=Path, help="Path to the recorder trajectory")
    bind.add_argument("--output", required=True, type=Path, help="Output submitted_pocs.json path")

    allp = sub.add_parser("all", help="Run all reasoning evaluators for one diagnostic bundle")
    allp.add_argument("--bundle", required=True, type=Path, help="Diagnostic bundle path")
    allp.add_argument("--output", type=Path, help="Output JSON path")
    allp.add_argument("--phase", choices=["pre_submit", "post_submit", "all"], default="pre_submit")

    args = parser.parse_args()
    _EVALUATORS = {"invariant": InvariantEvaluator, "t1": T1SourceSinkEvaluator, "t3": T3RootCauseEvaluator}
    if args.metric in _EVALUATORS:
        result = _EVALUATORS[args.metric]().evaluate(
            EvaluationInput(ground_truth=args.gt, trajectory=args.trajectory, output=args.output))
    elif args.metric == "t4":
        evaluator = T4PocSuccessEvaluator()
        result = evaluator.evaluate(EvaluationInput(ground_truth=Path(), trajectory=Path(), output=args.output, submitted_pocs=args.submitted_pocs))
    elif args.metric == "bind-pocs":
        attempts = write_bound_poc_attempts(args.trajectory, args.output)
        result = {
            "output": str(args.output),
            "trajectory": str(args.trajectory),
            "attempt_count": len(attempts),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return
    elif args.metric == "all":
        result = run_all(args.bundle, args.phase)
        if args.output is None:
            args.output = args.bundle / "evaluation" / "reasoning_eval.json"
    else:
        raise SystemExit(f"Unsupported metric: {args.metric}")

    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
