#!/usr/bin/env python3
"""Heuristic semantic review for ARVO ground-truth traces.

This is stricter than schema validation and artifact consistency checks. It
does not prove semantic correctness, but it flags GTs that are likely not a
faithful source-to-sink vulnerability explanation: fuzzer-entry sources,
crash-stack-only traces, disconnected depends_on chains, unsupported root
causes, and weak patch rationales.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

SOURCE_LOAD_KEYWORDS = {
    "read",
    "fread",
    "fgets",
    "getc",
    "open",
    "fopen",
    "parse",
    "decode",
    "load",
    "lex",
    "scan",
    "chunk",
    "input",
    "buffer",
    "apdu",
    "transmit",
    "metadata",
    "asn1",
    "stream",
}

GENERIC_VARS = {"runtime_state", "vulnerability_state", "crashing_access", "PoC input"}
ROOT_GROUNDING = {
    "patch",
    "patch_nearby",
    "free_context",
    "free_context_function",
    "free_context_nearby",
    "allocation_context",
    "allocation_context_function",
    "allocation_context_nearby",
    "sanitizer_origin",
}
SINK_GROUNDING = {"sanitizer_stack", "sanitizer_stack_function", "sanitizer_stack_nearby"}
MEMORY_ROOT_ROLES = {
    "root_cause",
    "allocation",
    "free",
    "invalid_free",
    "unsafe_allocation",
    "bounds_state",
    "lifetime_state",
    "stale_owner_lookup",
    "stale_reuse_decision",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def grounding_types(step: dict[str, Any]) -> set[str]:
    return {
        str(item.get("type"))
        for item in step.get("grounding", [])
        if isinstance(item, dict) and item.get("type")
    }


def text_blob(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts).lower()


def identifiers(value: str) -> set[str]:
    return {
        item
        for item in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", value or "")
        if len(item) > 1 and item not in {"int", "char", "const", "size_t", "unsigned", "struct"}
    }


def dep_is_supported(dep: str, previous_vars: list[str], previous_codes: list[str]) -> bool:
    if not dep:
        return False
    dep_ids = identifiers(dep)
    haystack = " ".join(previous_vars + previous_codes)
    if dep in previous_vars or dep in haystack:
        return True
    if dep_ids and dep_ids <= identifiers(haystack):
        return True
    # Permit common pointer/index projections when at least one identifier is
    # already present earlier in the trace.
    return bool(dep_ids and dep_ids & identifiers(haystack))


def review_sample(record: dict[str, Any], results: Path) -> dict[str, Any]:
    sid = record["local_sample_id"]
    gt_path = results / sid / "ground_truth.json"
    if not gt_path.exists():
        return {"sample_id": sid, "status": "fail", "errors": ["missing ground_truth.json"], "warnings": []}

    gt = load_json(gt_path)
    fine = gt.get("fine_trace") or []
    errors: list[str] = []
    warnings: list[str] = []

    if not fine:
        errors.append("fine_trace is empty")
        return {"sample_id": sid, "status": "fail", "errors": errors, "warnings": warnings}

    source = gt.get("source", {})
    source_step = fine[0]
    source_text = text_blob(
        source.get("file"),
        source.get("function"),
        source.get("description"),
        source_step.get("function"),
        source_step.get("var"),
        source_step.get("code"),
        source_step.get("note"),
    )
    source_function_text = text_blob(source.get("function"), source_step.get("function"))
    if "llvmfuzzertestoneinput" in source_function_text:
        errors.append("source is still the fuzzer entry instead of a project parser/load point")
    if not any(keyword in source_text for keyword in SOURCE_LOAD_KEYWORDS):
        warnings.append("source does not clearly describe an attacker input load/parse/materialization point")
    if source_step.get("role") != "source":
        errors.append("fine_trace does not start with a source role")

    roles = [str(step.get("role", "")) for step in fine]
    if "sink" not in roles:
        errors.append("fine_trace has no sink step")
    elif roles[-1] != "sink":
        warnings.append("fine_trace does not end at the sink")
    if "root_cause" not in roles:
        errors.append("fine_trace has no root_cause step")
    elif "sink" in roles and roles.index("root_cause") > roles.index("sink"):
        errors.append("root_cause appears after sink in fine_trace")

    if len(fine) < 5:
        warnings.append(f"fine_trace has only {len(fine)} steps; may be too shallow for T2")

    previous_vars: list[str] = []
    previous_codes: list[str] = []
    disconnected: list[int] = []
    generic_critical: list[int] = []
    for step in fine:
        step_no = step.get("step")
        role = str(step.get("role", ""))
        var = str(step.get("var", ""))
        code = str(step.get("code", ""))
        deps = step.get("depends_on")
        if not isinstance(deps, list):
            errors.append(f"step {step_no} depends_on is not a list")
            deps = []
        if role != "source" and not deps and role not in {"dispatch", "entry", "indirect_call"}:
            disconnected.append(step_no)
        for dep in deps:
            if not dep_is_supported(str(dep), previous_vars, previous_codes):
                warnings.append(f"step {step_no} dependency {dep!r} is not visibly produced by earlier steps")
        if role in {"root_cause", "sink", "free", "allocation", "unsafe_use"} and var in GENERIC_VARS:
            generic_critical.append(step_no)
        previous_vars.append(var)
        previous_codes.append(code)
    if disconnected:
        warnings.append(f"non-source steps with no depends_on: {disconnected}")
    if generic_critical:
        warnings.append(f"critical steps use generic vars: {generic_critical}")

    root_steps = [step for step in fine if step.get("role") == "root_cause"]
    if root_steps:
        root_ground = set().union(*(grounding_types(step) for step in root_steps))
        if not (root_ground & ROOT_GROUNDING):
            errors.append(f"root_cause lacks patch/free/allocation/origin grounding: {sorted(root_ground)}")

    sink_steps = [step for step in fine if step.get("role") == "sink"]
    if sink_steps and not any(grounding_types(step) & SINK_GROUNDING for step in sink_steps):
        errors.append("sink is not grounded by sanitizer_stack")
    elif sink_steps and not any("sanitizer_stack" in grounding_types(step) for step in sink_steps):
        warnings.append("sink is only grounded at sanitizer function/nearby-line level; exact file:line differs")

    stack_grounded_steps = sum(1 for step in fine if "sanitizer_stack" in grounding_types(step))
    non_stack_logic_steps = [
        step for step in fine
        if step.get("role") in MEMORY_ROOT_ROLES or not ("sanitizer_stack" in grounding_types(step))
    ]
    if len(fine) >= 4 and stack_grounded_steps >= len(fine) - 1 and len(non_stack_logic_steps) <= 1:
        warnings.append("trace looks crash-stack dominated rather than an explanatory data-flow chain")

    analysis = gt.get("root_cause_analysis") or {}
    why_patch = str(analysis.get("why_patch_works") or "").lower()
    summary = str(analysis.get("summary") or "").lower()
    if len(why_patch) < 80:
        warnings.append("why_patch_works is too short to justify root-cause repair")
    if not any(word in why_patch for word in ["prevent", "avoid", "reject", "check", "clear", "null", "assign", "free", "bounds", "size", "overflow", "stale"]):
        warnings.append("why_patch_works does not explain a concrete blocking mechanism")
    if "first-pass" in summary or "first-pass" in why_patch:
        errors.append("root_cause_analysis still contains first-pass wording")

    status = "pass"
    if errors:
        status = "fail"
    elif warnings:
        status = "warn"
    return {
        "sample_id": sid,
        "project": record.get("project"),
        "cwe": record.get("primary_category") or record.get("category"),
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "fine_steps": len(fine),
        "source": {
            "file": source.get("file"),
            "function": source.get("function"),
            "line": source.get("line"),
        },
        "root_cause": gt.get("root_cause", {}),
        "sink": gt.get("sink", {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, default=ROOT / "selected_samples_json/cybergym_overlap_50_final_gt_passed.json")
    parser.add_argument("--results", type=Path, default=ROOT / "gt_results")
    parser.add_argument("--output", type=Path, default=ROOT / "gt_results/cybergym_arvo50_semantic_review.json")
    parser.add_argument("--sample-id", action="append", default=[])
    args = parser.parse_args()

    records = load_json(args.selection)
    if args.sample_id:
        wanted = set(args.sample_id)
        records = [record for record in records if record["local_sample_id"] in wanted]

    samples = [review_sample(record, args.results) for record in records]
    counts: dict[str, int] = {}
    for item in samples:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    report = {"total": len(samples), "counts": counts, "samples": samples}
    write_json(args.output, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
