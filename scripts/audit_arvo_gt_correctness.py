#!/usr/bin/env python3
"""Audit CyberGym/ARVO GT files against runtime and patch artifacts.

This is not a semantic proof of the vulnerability explanation. It is a
deterministic consistency audit that finds GTs whose source/sink/root-cause
claims are not grounded by the artifacts we keep: sanitizer trace, structured
sanitizer GT, patch.diff, and post-patch differential output.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
# gt_toolkit lives under gt_generation/; put it on PYTHONPATH so `-m gt_toolkit` resolves.
os.environ["PYTHONPATH"] = str(ROOT / "gt_generation") + os.pathsep + os.environ.get("PYTHONPATH", "")
STRONG_ROOT_GROUNDING = {"patch", "free_context", "allocation_context"}
LOAD_POINT_KEYWORDS = {
    "fopen",
    "open(",
    "fdopen",
    "gzopen",
    "read(",
    "fread",
    "getc",
    "getchar",
    "ifstream",
    "istream",
    "ByteStream",
    "Read",
    "read",
    "parse",
    "Parse",
    "decode",
    "Decode",
}


def norm_file(path: str | None) -> str:
    if not path:
        return ""
    p = path.replace("\\", "/")
    for marker in ["/src/", "/build_sanitizer/", "/build_debug/", "/build_valgrind/"]:
        if marker in p:
            p = p.split(marker, 1)[1]
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p.lstrip("/")


def same_file(a: str | None, b: str | None) -> bool:
    a = norm_file(a)
    b = norm_file(b)
    return bool(a and b and (a == b or a.endswith("/" + b) or b.endswith("/" + a)))


def same_loc(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return same_file(a.get("file"), b.get("file")) and a.get("line") == b.get("line")


def grounding_types(step: dict[str, Any]) -> set[str]:
    return {str(item.get("type")) for item in step.get("grounding", []) if isinstance(item, dict)}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def schema_ok(gt_path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["python3", "-m", "gt_toolkit", "validate", str(gt_path)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc.returncode == 0, proc.stdout


def classify_status(errors: list[str], warnings: list[str]) -> str:
    if errors:
        return "fail"
    if warnings:
        return "warn"
    return "pass"


def audit_sample(record: dict[str, Any], results: Path) -> dict[str, Any]:
    sid = record["local_sample_id"]
    d = results / sid
    errors: list[str] = []
    warnings: list[str] = []

    gt_path = d / "ground_truth.json"
    state_path = d / "sample_state.json"
    patch_path = d / "patch.diff"
    sanitizer_trace_path = d / "sanitizer_trace.txt"
    post_patch_trace_path = d / "post_patch_trace.txt"

    for path in [gt_path, state_path, patch_path, sanitizer_trace_path, post_patch_trace_path]:
        if not path.exists():
            errors.append(f"missing artifact: {path.name}")
    if errors:
        return {"sample_id": sid, "status": "fail", "errors": errors, "warnings": warnings}

    ok, schema_out = schema_ok(gt_path)
    if not ok:
        errors.append(f"schema failed: {schema_out.strip()[:500]}")

    gt = read_json(gt_path)
    state = read_json(state_path)
    san = gt.get("sanitizer_ground_truth", {})
    fine = gt.get("fine_trace", [])

    if state.get("status") != "gt_completed_schema_passed":
        errors.append(f"sample_state status is {state.get('status')}")

    sanitizer_trace = sanitizer_trace_path.read_text(errors="replace")
    post_patch_trace = post_patch_trace_path.read_text(errors="replace")
    if "ERROR: AddressSanitizer" not in sanitizer_trace and "WARNING: MemorySanitizer" not in sanitizer_trace:
        errors.append("vulnerable trace does not contain sanitizer crash marker")
    if "ERROR: AddressSanitizer" in post_patch_trace or "WARNING: MemorySanitizer" in post_patch_trace:
        errors.append("post-patch trace still contains sanitizer crash marker")
    if not patch_path.read_text(errors="replace").strip():
        errors.append("patch.diff is empty")

    expected_cwe = record.get("cwe") or record.get("category")
    if expected_cwe == "CWE-590/763":
        expected_cwe = "CWE-590/CWE-763"
    actual_cwe = gt.get("classification", {}).get("cwe")
    if actual_cwe != expected_cwe:
        errors.append(f"classification.cwe {actual_cwe!r} != selected CWE {expected_cwe!r}")
    if "," in str(actual_cwe):
        errors.append("classification.cwe contains multiple comma-separated CWE buckets")

    crash = san.get("crash_location", {})
    sink = gt.get("sink", {})
    root = gt.get("root_cause", {})
    cross_validation = san.get("cross_validation") if isinstance(san.get("cross_validation"), dict) else {}
    if not same_loc(sink, crash):
        errors.append("sink does not match sanitizer_ground_truth.crash_location")
    if cross_validation.get("sink_matches_crash") is not True:
        errors.append(f"cross_validation.sink_matches_crash is not true: {cross_validation.get('sink_matches_crash')!r}")
    if cross_validation.get("trace_consistent_with_stack") is not True:
        errors.append(f"cross_validation.trace_consistent_with_stack is not true: {cross_validation.get('trace_consistent_with_stack')!r}")
    if cross_validation.get("tainted_value_reaches_sink") is not True:
        errors.append(f"cross_validation.tainted_value_reaches_sink is not true: {cross_validation.get('tainted_value_reaches_sink')!r}")

    sink_steps = [s for s in fine if s.get("role") == "sink"]
    if not sink_steps:
        errors.append("fine_trace has no sink step")
    elif not any("sanitizer_stack" in grounding_types(s) for s in sink_steps):
        errors.append("sink step is not grounded by sanitizer_stack")

    root_steps = [s for s in fine if s.get("role") == "root_cause"]
    if not root_steps:
        errors.append("fine_trace has no root_cause step")
    else:
        root_ground = set().union(*(grounding_types(s) for s in root_steps))
        if not (root_ground & STRONG_ROOT_GROUNDING):
            errors.append(f"root_cause lacks strong grounding: {sorted(root_ground)}")
        if same_loc(root, sink) and not (root_ground & {"patch", "free_context", "allocation_context"}):
            errors.append("root_cause is identical to sink without patch/free/allocation grounding")
        if "requires review" in str(root.get("description", "")).lower():
            errors.append("root_cause description says requires review")
        root_desc = str(root.get("description", "")).lower()
        if "first-pass" in root_desc or "selected as" in root_desc or "most concrete runtime root-cause anchor" in root_desc:
            errors.append("root_cause still has bootstrap heuristic wording")

    source_steps = [s for s in fine if s.get("role") == "source"]
    if not source_steps:
        errors.append("fine_trace has no source step")
    else:
        source_text = " ".join(
            str(source_steps[0].get(k, ""))
            for k in ["function", "var", "code", "note"]
        )
        if not any(keyword in source_text for keyword in LOAD_POINT_KEYWORDS):
            warnings.append("source step does not look like an input load/parse point")
        if str(source_steps[0].get("function")) == "LLVMFuzzerTestOneInput":
            errors.append("source is LLVMFuzzerTestOneInput; fuzzer entry is an unscored boundary, not the scored parser/materialization source")
        if source_steps[0].get("var") in {"runtime_state", "PoC input"}:
            errors.append("source variable is generic; source should name loaded input buffer/file/stream/value")

    if fine:
        first_role = str(fine[0].get("role", ""))
        if first_role != "source":
            warnings.append(f"fine_trace does not start with source step: first role is {first_role}")
        if str(fine[-1].get("role", "")) != "sink":
            warnings.append("fine_trace does not end with sink step")

    if len(fine) <= 3:
        warnings.append(f"fine_trace is very short ({len(fine)} steps)")

    asserted_critical = [
        s.get("role")
        for s in fine
        if s.get("role") in {"root_cause", "sink", "free", "allocation", "unsafe_use"}
        and grounding_types(s) == {"asserted"}
    ]
    if asserted_critical:
        warnings.append(f"critical steps only asserted: {asserted_critical}")

    patch_grounded = any("patch" in grounding_types(s) for s in fine)
    if not patch_grounded:
        errors.append("no fine_trace step is grounded by patch.diff")

    return {
        "sample_id": sid,
        "project": record.get("project"),
        "cwe": expected_cwe,
        "status": classify_status(errors, warnings),
        "errors": errors,
        "warnings": warnings,
        "crash_type": san.get("crash_type"),
        "root_cause": {
            "file": root.get("file"),
            "function": root.get("function"),
            "line": root.get("line"),
        },
        "sink": {
            "file": sink.get("file"),
            "function": sink.get("function"),
            "line": sink.get("line"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, default=ROOT / "selected_samples_json" / "cybergym_overlap_50_effective.json")
    parser.add_argument("--results", type=Path, default=ROOT / "gt_results")
    parser.add_argument("--output", type=Path, default=ROOT / "gt_results" / "cybergym_arvo50_gt_audit.json")
    args = parser.parse_args()

    records = read_json(args.selection)
    samples = [audit_sample(record, args.results) for record in records]
    counts = {}
    for item in samples:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    report = {"total": len(samples), "counts": counts, "samples": samples}
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
