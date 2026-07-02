#!/usr/bin/env python3
"""Run deterministic T1-T5 evaluators.

Only T2 is implemented initially because propagation-trace recovery is the
hardest metric and should not use LLM-as-a-judge.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .base import EvaluationInput
from .poc_attempt_binder import write_bound_poc_attempts
from .t1_source_sink import T1SourceSinkEvaluator
from .t2_trace import T2PropagationTraceEvaluator
from .t3_root_cause import T3RootCauseEvaluator
from .t4_poc_success import T4PocSuccessEvaluator
from .t5_rationale import T5RationaleEvaluator


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
        "t1": T1SourceSinkEvaluator(phase=phase).evaluate(inputs),
        "t2": T2PropagationTraceEvaluator(phase=phase).evaluate(inputs),
        "t3": T3RootCauseEvaluator(phase=phase).evaluate(inputs),
        "t4": T4PocSuccessEvaluator().evaluate(inputs),
        "t5": T5RationaleEvaluator(phase=phase).evaluate(inputs),
    }
    t1_structured = bool(metrics["t1"].get("structured_reasoning_evaluable"))
    t2_structured = bool(metrics["t2"].get("trajectory", {}).get("structured_reasoning_evaluable"))
    t3_structured = bool(metrics["t3"].get("structured_reasoning_evaluable"))
    return {
        "bundle": str(bundle),
        "phase_policy": phase,
        "metrics": metrics,
        "summary": {
            "structured_reasoning_evaluable": t1_structured and t2_structured and t3_structured,
            "t1_structured_evaluable": t1_structured,
            "t2_structured_evaluable": t2_structured,
            "t3_structured_evaluable": t3_structured,
            "t1_strict_source_sink_identified": metrics["t1"]["summary"]["strict_source_sink_identified"] if t1_structured else None,
            "t2_strict_step_recall": metrics["t2"]["summary"]["strict_step_recall"] if t2_structured else None,
            "t2_lenient_step_recall": metrics["t2"]["summary"]["lenient_step_recall"] if t2_structured else None,
            "t2_strict_edge_recall": metrics["t2"]["summary"]["strict_edge_recall"] if t2_structured else None,
            "t2_lenient_edge_recall": metrics["t2"]["summary"]["lenient_edge_recall"] if t2_structured else None,
            "t3_strict_root_cause_understood": metrics["t3"]["summary"]["strict_root_cause_understood"] if t3_structured else None,
            "t4_cybergym_poc_success": metrics["t4"]["summary"]["cybergym_poc_success"],
            "t5_root_cause_rationale_seen": metrics["t5"]["summary"]["root_cause_rationale_seen"],
            "trajectory_diagnostic": {
                "t1_strict_source_sink_identified": metrics["t1"]["summary"]["strict_source_sink_identified"],
                "t2_strict_step_recall": metrics["t2"]["summary"]["strict_step_recall"],
                "t2_lenient_step_recall": metrics["t2"]["summary"]["lenient_step_recall"],
                "t2_strict_edge_recall": metrics["t2"]["summary"]["strict_edge_recall"],
                "t2_lenient_edge_recall": metrics["t2"]["summary"]["lenient_edge_recall"],
                "t3_strict_root_cause_understood": metrics["t3"]["summary"]["strict_root_cause_understood"],
            } if not (t1_structured and t2_structured and t3_structured) else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="metric", required=True)

    t2 = sub.add_parser("t2", help="Evaluate propagation trace recovery")
    t2.add_argument("--gt", required=True, type=Path, help="Path to ground_truth.json")
    t2.add_argument("--trajectory", required=True, type=Path, help="Path to OpenHands trajectory")
    t2.add_argument("--output", type=Path, help="Output JSON path")
    t2.add_argument(
        "--phase",
        choices=["pre_submit", "post_submit", "all"],
        default="pre_submit",
        help="Trajectory phase to score. Default avoids crediting post-submit ASan stack text.",
    )

    for metric in ["t1", "t3", "t5"]:
        p = sub.add_parser(metric, help=f"Evaluate {metric.upper()}")
        p.add_argument("--gt", required=True, type=Path, help="Path to ground_truth.json")
        p.add_argument("--trajectory", required=True, type=Path, help="Path to OpenHands trajectory")
        p.add_argument("--output", type=Path, help="Output JSON path")
        p.add_argument("--phase", choices=["pre_submit", "post_submit", "all"], default="pre_submit")

    t4 = sub.add_parser("t4", help="Evaluate CyberGym PoC success")
    t4.add_argument("--submitted-pocs", required=True, type=Path, help="Path to submitted_pocs.json")
    t4.add_argument("--output", type=Path, help="Output JSON path")

    bind = sub.add_parser("bind-pocs", help="Bind submit.sh attempts to pre-submit reasoning state")
    bind.add_argument("--trajectory", required=True, type=Path, help="Path to OpenHands trajectory")
    bind.add_argument("--output", required=True, type=Path, help="Output submitted_pocs.json path")

    allp = sub.add_parser("all", help="Run all implemented T1-T5 evaluators for one diagnostic bundle")
    allp.add_argument("--bundle", required=True, type=Path, help="Diagnostic bundle path")
    allp.add_argument("--output", type=Path, help="Output JSON path")
    allp.add_argument("--phase", choices=["pre_submit", "post_submit", "all"], default="pre_submit")

    args = parser.parse_args()
    if args.metric == "t2":
        evaluator = T2PropagationTraceEvaluator(phase=args.phase)
        result = evaluator.evaluate(EvaluationInput(ground_truth=args.gt, trajectory=args.trajectory, output=args.output))
    elif args.metric == "t1":
        evaluator = T1SourceSinkEvaluator(phase=args.phase)
        result = evaluator.evaluate(EvaluationInput(ground_truth=args.gt, trajectory=args.trajectory, output=args.output))
    elif args.metric == "t3":
        evaluator = T3RootCauseEvaluator(phase=args.phase)
        result = evaluator.evaluate(EvaluationInput(ground_truth=args.gt, trajectory=args.trajectory, output=args.output))
    elif args.metric == "t4":
        evaluator = T4PocSuccessEvaluator()
        result = evaluator.evaluate(EvaluationInput(ground_truth=Path(), trajectory=Path(), output=args.output, submitted_pocs=args.submitted_pocs))
    elif args.metric == "t5":
        evaluator = T5RationaleEvaluator(phase=args.phase)
        result = evaluator.evaluate(EvaluationInput(ground_truth=args.gt, trajectory=args.trajectory, output=args.output))
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
            args.output = args.bundle / "evaluation" / "t1_t5_eval.json"
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
