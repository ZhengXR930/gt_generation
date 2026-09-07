"""Ground-truth assisted reference trajectory synthesis.

Reference trajectories are label-side distillation inputs. They are meant for
Diagnostician/Teacher credit assignment, not for the coding agent and not for
verbatim skill text. The synthetic form below intentionally captures actions,
context, and semantic candidate constraints without copying raw PoC bytes.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import re
from typing import Any

from .artifacts import GT_RESULTS, load_json_if_exists, write_json

REFERENCE_PROTOCOL = "reward-reference-trajectory-v1"


_HEX_BYTE_SEQ_RE = re.compile(r"(?i)(?:\b[0-9a-f]{2}\b[\s,;:-]*){6,}")
_EXACT_BLOB_RE = re.compile(r"(?i)(exact\s+\d+[- ]byte\s+(?:blob|witness|file|input)\s*)`[^`]+`")


def _redact_poc_material(text: str) -> str:
    text = _EXACT_BLOB_RE.sub(r"\1[redacted raw PoC bytes]", text)
    text = _HEX_BYTE_SEQ_RE.sub("[redacted raw PoC bytes]", text)
    return text


def _short(value: Any, limit: int = 700) -> str:
    text = _redact_poc_material(" ".join(str(value or "").split()))
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _stage_for_gt_step(step: dict[str, Any], gt: dict[str, Any]) -> str:
    index = step.get("step")
    # Exact trace_step and exact file/function/line matches win first. Sink and
    # root cause often share one function, so function-only matching is last.
    for name in ("source", "root_cause", "sink"):
        node = gt.get(name) or {}
        if node.get("trace_step") == index:
            return name
    for name in ("sink", "root_cause", "source"):
        node = gt.get(name) or {}
        if (
            node.get("file") == step.get("file")
            and node.get("function") == step.get("function")
            and node.get("line") == step.get("line")
        ):
            return name
    for name in ("source", "root_cause", "sink"):
        node = gt.get(name) or {}
        if node.get("file") == step.get("file") and node.get("function") == step.get("function"):
            return name
    if index == 1:
        return "parser_or_source"
    return "propagation"


def _reference_step(step: dict[str, Any], gt: dict[str, Any]) -> dict[str, Any]:
    depends = []
    for dep in step.get("depends_on") or []:
        if isinstance(dep, dict):
            depends.append({
                "on_step": dep.get("on"),
                "type": dep.get("type"),
                "via": dep.get("via"),
            })
    return {
        "step": step.get("step"),
        "stage": _stage_for_gt_step(step, gt),
        "file": step.get("file"),
        "function": step.get("function"),
        "line": step.get("line"),
        "variable": step.get("var"),
        "code": _short(step.get("code"), 220),
        "goal": _short(step.get("note") or step.get("description"), 450),
        "depends_on": depends,
    }


def _unique_locations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: "OrderedDict[tuple[str, str, int | None], dict[str, Any]]" = OrderedDict()
    for item in items:
        file = item.get("file") or item.get("expected_file")
        function = item.get("function") or item.get("expected_function")
        line = item.get("line") if item.get("line") is not None else item.get("expected_line")
        if not file or not function:
            continue
        try:
            norm_line = int(line) if line is not None else None
        except (TypeError, ValueError):
            norm_line = None
        key = (str(file), str(function), norm_line)
        if key not in seen:
            seen[key] = {
                "kind": "function_visit",
                "file": str(file),
                "function": str(function),
                "line": norm_line,
                "visit_count": 0,
            }
        seen[key]["visit_count"] += int(item.get("hit_count") or 1)
    return list(seen.values())


def _context_targets(context_gt: dict[str, Any] | None, gt: dict[str, Any], limit: int = 80) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    # Put canonical GT paths first; dynamic backtraces may contain basenames.
    for key in ("source", "tainted_value_origin", "root_cause", "sink"):
        node = gt.get(key) or {}
        if isinstance(node, dict):
            raw.append(node)
    for step in gt.get("fine_trace") or []:
        if isinstance(step, dict):
            raw.append(step)
    if isinstance(context_gt, dict):
        for item in context_gt.get("context") or []:
            if isinstance(item, dict):
                raw.append(item)
        for event in context_gt.get("events") or []:
            if not isinstance(event, dict):
                continue
            hit = event.get("hit") or {}
            if isinstance(hit, dict):
                raw.append({**hit, "hit_count": event.get("hit_count")})
            for frame in event.get("stack") or []:
                if isinstance(frame, dict):
                    raw.append(frame)
    return _unique_locations(raw)[:limit]


def _candidate_constraints(gt: dict[str, Any]) -> list[dict[str, Any]]:
    constraints: list[dict[str, Any]] = []
    bug = gt.get("bug_description") or {}
    if isinstance(bug, dict) and bug.get("normalized"):
        constraints.append({
            "kind": "issue_semantics",
            "description": _short(bug.get("normalized"), 900),
        })
    source = gt.get("source") or {}
    if isinstance(source, dict):
        constraints.append({
            "kind": "source_control",
            "file": source.get("file"),
            "function": source.get("function"),
            "line": source.get("line"),
            "description": _short(source.get("value_from") or source.get("description"), 650),
        })
    root = gt.get("root_cause") or {}
    if isinstance(root, dict):
        constraints.append({
            "kind": "root_condition",
            "file": root.get("file"),
            "function": root.get("function"),
            "line": root.get("line"),
            "description": _short(root.get("description") or root.get("note"), 650),
            "relation": root.get("relation"),
        })
    sink = gt.get("sink") or {}
    if isinstance(sink, dict):
        constraints.append({
            "kind": "trigger_observation",
            "file": sink.get("file"),
            "function": sink.get("function"),
            "line": sink.get("line"),
            "description": _short(sink.get("description") or sink.get("note"), 650),
            "relation": sink.get("relation"),
        })
    return constraints


def _expected_feedback(reach: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(reach, dict):
        return {"available": False}
    keys = [
        "R1_parser_admitted",
        "R2_source_reached",
        "R3_root_cause_function_reached",
        "R3_sink_function_reached",
        "R4_root_cause_line_reached",
        "R4_sink_line_reached",
        "R5_sanitizer_triggered",
        "failure_stage",
    ]
    return {"available": True, **{key: reach.get(key) for key in keys if key in reach}}


def synthesize_reference(sample_id: str) -> dict[str, Any]:
    sample_dir = GT_RESULTS / sample_id
    gt = load_json_if_exists(sample_dir / "ground_truth.json") or {}
    if not isinstance(gt, dict) or not gt:
        raise FileNotFoundError(f"missing ground_truth.json for {sample_id}")
    context_gt = load_json_if_exists(sample_dir / "context_gt.json") or load_json_if_exists(sample_dir / "context" / "context_gt.json")
    invariants = load_json_if_exists(sample_dir / "verified_invariants.json") or {}
    reach = load_json_if_exists(sample_dir / "reachability_report.json") or {}
    description_path = sample_dir / "description.txt"
    description = description_path.read_text(encoding="utf-8").strip() if description_path.is_file() else ""
    project = gt.get("project") or {}
    classification = gt.get("classification") or {}
    trace = [_reference_step(step, gt) for step in (gt.get("fine_trace") or []) if isinstance(step, dict)]
    invariant_nodes = []
    if isinstance(invariants, dict):
        for node in invariants.get("nodes") or []:
            if isinstance(node, dict):
                invariant_nodes.append({
                    "role": node.get("role"),
                    "file": node.get("file"),
                    "function": node.get("function"),
                    "line": node.get("line"),
                    "description": _short(node.get("description"), 360),
                })
    return {
        "protocol": REFERENCE_PROTOCOL,
        "sample_id": sample_id,
        "source": {
            "mode": "gt_assisted_synthetic",
            "inputs": [
                "description.txt",
                "ground_truth.json",
                "verified_invariants.json",
                "reachability_report.json",
                "context_gt.json",
            ],
            "visibility": "distillation_roles_only",
            "raw_poc_bytes_included": False,
            "redaction_policy": "Exact raw PoC byte sequences are redacted from free-text GT fields before this reference is exposed.",
            "skill_text_policy": "Teacher and Curator may use this for diagnosis, but accepted lessons must not quote sample-specific constants, PoC bytes, file names, line numbers, or commit ids.",
        },
        "task_context": {
            "issue_description": description,
            "project": project.get("id") or project.get("name"),
            "fuzz_target": project.get("fuzz_target"),
            "vulnerability_class": classification.get("class"),
            "detector": classification.get("detector"),
        },
        "reference_trace": trace,
        "candidate_constraints": _candidate_constraints(gt),
        "expected_feedback": _expected_feedback(reach),
        "context_targets": _context_targets(context_gt if isinstance(context_gt, dict) else None, gt),
        "invariant_targets": invariant_nodes,
        "diagnostic_use": {
            "compare_against_failed_trajectory": [
                "first missing or wrong inspected context",
                "first semantic candidate constraint the agent did not test",
                "whether feedback prompted a useful revised candidate",
            ],
            "do_not_use_as": [
                "coding-agent prompt",
                "verbatim skill lesson",
                "direct replacement for trajectory evidence",
            ],
        },
    }


def synthesize_reference_file(sample_id: str, out_file: Path) -> dict[str, Any]:
    reference = synthesize_reference(sample_id)
    write_json(out_file, reference)
    return reference
