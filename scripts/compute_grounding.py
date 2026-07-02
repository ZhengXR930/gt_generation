#!/usr/bin/env python3
"""Compute per-step grounding from artifacts, not from model assertions.

This script intentionally overwrites fine_trace[*].grounding. The GT generator
may produce trace steps, depends_on, and notes, but grounding provenance must be
derived deterministically from structured oracle facts: sanitizer_ground_truth,
and patch hunks.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re


PATCH_GROUNDABLE_ROLES = {
    "root_cause",
    "sink",
    "unsafe_allocation",
    "undersized_allocation",
    "bounds_state",
    "lifetime_state",
    "free",
    "free_path",
    "invalid_free",
    "dangling_pointer_use",
    "stale_reuse_decision",
    "unsafe_use",
    "invalid_cast",
}
ALLOCATION_GROUNDABLE_ROLES = {
    "allocation",
    "unsafe_allocation",
    "undersized_allocation",
    "root_cause",
    "bounds_state",
}
FREE_GROUNDABLE_ROLES = {
    "free",
    "first_free",
    "free_path",
    "invalid_free",
    "root_cause",
    "unsafe_use",
    "dangling_pointer_use",
    "sink",
}
STACK_FUNCTION_GROUNDABLE_ROLES = {
    "root_cause",
    "sink",
    "unsafe_use",
    "invalid_free",
    "free",
    "free_path",
    "dangling_pointer_use",
    "tainted_value_origin",
}


@dataclass(frozen=True)
class Loc:
    file: str
    line: int
    function: str = ""


def norm_file(path: str | None) -> str:
    if not path:
        return ""
    path = path.replace("\\", "/")
    markers = ["/build_sanitizer/", "/build_valgrind/", "/build_debug/", "/build_patched/", "/src/"]
    for marker in markers:
        if marker in path:
            path = path.split(marker, 1)[1]
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path.lstrip("/")


def same_file(a: str, b: str) -> bool:
    a = norm_file(a)
    b = norm_file(b)
    return bool(a and b and (a == b or a.endswith("/" + b) or b.endswith("/" + a)))


def loc_matches(step: dict[str, Any], loc: dict[str, Any] | None) -> bool:
    if not isinstance(loc, dict):
        return False
    return same_file(str(step.get("file", "")), str(loc.get("file", ""))) and step.get("line") == loc.get("line")


def loc_matches_function_nearby(step: dict[str, Any], loc: Loc, tolerance: int = 10) -> bool:
    """Match imprecise sanitizer/debug locations without pretending exactness.

    Some ARVO/OSS-Fuzz traces point at a function prologue or at an adjacent
    line from the instrumented source tree while the public vulnerable checkout
    has the responsible statement a few lines away.  This produces a weaker
    grounding label than an exact file:line match.
    """
    if not same_file(str(step.get("file", "")), loc.file):
        return False
    if str(step.get("function", "")) != loc.function:
        return False
    try:
        step_line = int(step.get("line"))
    except Exception:
        return False
    return abs(step_line - loc.line) <= tolerance


def loc_matches_file_nearby(step: dict[str, Any], loc: Loc, tolerance: int = 10) -> bool:
    if not same_file(str(step.get("file", "")), loc.file):
        return False
    try:
        step_line = int(step.get("line"))
    except Exception:
        return False
    return abs(step_line - loc.line) <= tolerance


def loc_matches_same_function(step: dict[str, Any], loc: Loc) -> bool:
    return (
        same_file(str(step.get("file", "")), loc.file)
        and bool(loc.function)
        and str(step.get("function", "")) == loc.function
    )


def evidence(kind: str, strength: str, text: str) -> dict[str, str]:
    return {"type": kind, "strength": strength, "evidence": text}


def compact_code(text: str | None) -> str:
    text = re.sub(r"//.*", "", text or "")
    text = re.sub(r"/\*.*?\*/", "", text)
    return re.sub(r"\s+", " ", text).strip().rstrip(";")


def parse_patch_hunks(patch_path: Path) -> tuple[set[Loc], set[Loc], dict[str, set[str]], dict[str, set[str]]]:
    if not patch_path.exists():
        raise FileNotFoundError(f"patch diff not found: {patch_path}")

    changed: set[Loc] = set()
    hunk_lines: set[Loc] = set()
    changed_code: dict[str, set[str]] = {}
    hunk_code: dict[str, set[str]] = {}

    old_file = ""
    old_line: int | None = None
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
    for raw in patch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("--- "):
            old_file = norm_file(raw[4:].split("\t", 1)[0].strip())
            continue
        match = hunk_re.match(raw)
        if match:
            old_line = int(match.group(1))
            continue
        if old_line is None or not old_file or old_file == "/dev/null" or not raw:
            continue
        marker = raw[0]
        if marker not in {" ", "-"}:
            continue
        loc = Loc(old_file, old_line)
        hunk_lines.add(loc)
        code = compact_code(raw[1:])
        if code:
            hunk_code.setdefault(old_file, set()).add(code)
        if marker == "-":
            changed.add(loc)
            if code:
                changed_code.setdefault(old_file, set()).add(code)
        old_line += 1

    return changed, hunk_lines, changed_code, hunk_code


def line_matches(step: dict[str, Any], loc: Loc, tolerance: int = 0) -> bool:
    if not same_file(str(step.get("file", "")), loc.file):
        return False
    try:
        step_line = int(step.get("line"))
        step_line_end = int(step.get("line_end", step_line))
    except Exception:
        return False
    if step_line <= loc.line <= step_line_end:
        return True
    return abs(step_line - loc.line) <= tolerance


def loc_set_contains(step_loc: Loc, locs: set[Loc]) -> bool:
    return any(step_loc.line == loc.line and same_file(step_loc.file, loc.file) for loc in locs)


def loc_set_nearby(step_loc: Loc, locs: set[Loc], tolerance: int = 10) -> bool:
    return any(abs(step_loc.line - loc.line) <= tolerance and same_file(step_loc.file, loc.file) for loc in locs)


def step_loc_or_none(step: dict[str, Any]) -> Loc | None:
    try:
        line = int(step.get("line"))
    except Exception:
        return None
    file = norm_file(str(step.get("file", "")))
    if not file or line <= 0:
        return None
    return Loc(file, line)


def patch_code_matches(step: dict[str, Any], code_map: dict[str, set[str]]) -> bool:
    step_file = norm_file(str(step.get("file", "")))
    step_code = compact_code(str(step.get("code", "")))
    if not step_file or not step_code:
        return False
    for file, snippets in code_map.items():
        if not same_file(step_file, file):
            continue
        for snippet in snippets:
            if snippet and (snippet in step_code or step_code in snippet):
                return True
    return False


def grounding_types(step: dict[str, Any]) -> set[str]:
    return {
        str(item.get("type"))
        for item in step.get("grounding", [])
        if isinstance(item, dict)
    }


def same_loc(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return same_file(str(a.get("file", "")), str(b.get("file", ""))) and a.get("line") == b.get("line")


def is_harness_source(source: dict[str, Any] | None, source_step: dict[str, Any] | None) -> bool:
    fn = str((source or {}).get("function") or (source_step or {}).get("function") or "")
    var = str((source_step or {}).get("var") or "")
    if fn == "LLVMFuzzerTestOneInput":
        return True
    return var in {"runtime_state", "PoC input"}


def update_cross_validation(data: dict[str, Any]) -> None:
    sanitizer_gt = data.setdefault("sanitizer_ground_truth", {})
    cross_validation = sanitizer_gt.setdefault("cross_validation", {})
    fine = [step for step in data.get("fine_trace", []) if isinstance(step, dict)]
    roles = [str(step.get("role", "")) for step in fine]
    sink_steps = [step for step in fine if step.get("role") == "sink"]
    source_steps = [step for step in fine if step.get("role") == "source"]

    sink_matches_crash = same_loc(data.get("sink"), sanitizer_gt.get("crash_location"))
    sink_stack_grounded = any("sanitizer_stack" in grounding_types(step) for step in sink_steps)
    trace_shape_ok = (
        bool(source_steps)
        and bool(sink_steps)
        and bool(fine)
        and fine[0].get("role") == "source"
        and fine[-1].get("role") == "sink"
        and "root_cause" in roles
        and not is_harness_source(data.get("source"), source_steps[0] if source_steps else None)
    )
    critical_roles = {"root_cause", "sink", "free", "allocation", "unsafe_use"}
    critical_only_asserted = any(
        step.get("role") in critical_roles and grounding_types(step) == {"asserted"}
        for step in fine
    )

    cross_validation["sink_matches_crash"] = sink_matches_crash
    cross_validation["trace_consistent_with_stack"] = bool(sink_matches_crash and sink_stack_grounded)
    cross_validation["tainted_value_reaches_sink"] = bool(trace_shape_ok and not critical_only_asserted)
    cross_validation["computed_by"] = "compute_grounding.py"


def compute(data: dict[str, Any], patch_path: Path) -> dict[str, Any]:
    changed_lines, hunk_lines, changed_code, hunk_code = parse_patch_hunks(patch_path)
    sanitizer_gt = data.get("sanitizer_ground_truth", {})

    crash_stack = sanitizer_gt.get("crash_stack") or []
    stack_locs = [
        Loc(norm_file(frame.get("file", "")), int(frame.get("line")), str(frame.get("function", "")))
        for frame in crash_stack
        if isinstance(frame, dict) and isinstance(frame.get("line"), int)
    ]
    allocation_stack_locs = [
        Loc(norm_file(frame.get("file", "")), int(frame.get("line")), str(frame.get("function", "")))
        for frame in (sanitizer_gt.get("allocation_stack") or [])
        if isinstance(frame, dict) and isinstance(frame.get("line"), int)
    ]
    origin_stack_locs = [
        Loc(norm_file(frame.get("file", "")), int(frame.get("line")), str(frame.get("function", "")))
        for frame in (sanitizer_gt.get("origin_stack") or [])
        if isinstance(frame, dict) and isinstance(frame.get("line"), int)
    ]
    free_stack_locs = [
        Loc(norm_file(frame.get("file", "")), int(frame.get("line")), str(frame.get("function", "")))
        for frame in (sanitizer_gt.get("free_stack") or [])
        if isinstance(frame, dict) and isinstance(frame.get("line"), int)
    ]

    for step in data.get("fine_trace", []):
        grounding: list[dict[str, str]] = []

        role = str(step.get("role", ""))
        if any(line_matches(step, loc) for loc in stack_locs):
            grounding.append(evidence("sanitizer_stack", "strong", "Step file/line appears in sanitizer crash_stack."))
        elif any(loc_matches_function_nearby(step, loc) for loc in stack_locs):
            grounding.append(evidence("sanitizer_stack_function", "medium", "Step is in the same sanitizer crash-stack function within a small source-line drift window."))
        elif role in STACK_FUNCTION_GROUNDABLE_ROLES and any(loc_matches_same_function(step, loc) for loc in stack_locs):
            grounding.append(evidence("sanitizer_stack_function", "weak", "Step is in the same sanitizer crash-stack function; exact source line differs substantially."))
        elif any(loc_matches_file_nearby(step, loc, tolerance=15) for loc in stack_locs):
            grounding.append(evidence("sanitizer_stack_nearby", "weak", "Step is in the same file and near a sanitizer crash-stack line; function or exact line differs."))

        if role in ALLOCATION_GROUNDABLE_ROLES:
            if loc_matches(step, sanitizer_gt.get("allocation_context")):
                grounding.append(evidence("allocation_context", "strong", "Step exactly matches sanitizer allocation_context."))
            elif any(line_matches(step, loc) for loc in allocation_stack_locs):
                grounding.append(evidence("allocation_context", "strong", "Step appears in sanitizer allocation_stack."))
            elif any(loc_matches_function_nearby(step, loc) for loc in allocation_stack_locs):
                grounding.append(evidence("allocation_context_function", "medium", "Step is in the same sanitizer allocation-stack function within a small source-line drift window."))
            elif any(loc_matches_file_nearby(step, loc) for loc in allocation_stack_locs):
                grounding.append(evidence("allocation_context_nearby", "weak", "Step is in the same file and near a sanitizer allocation-stack line; function or exact line differs."))

        if any(line_matches(step, loc) for loc in origin_stack_locs):
            grounding.append(evidence("sanitizer_origin", "strong", "Step appears in sanitizer origin_stack."))

        if role in FREE_GROUNDABLE_ROLES:
            if loc_matches(step, sanitizer_gt.get("free_context")):
                grounding.append(evidence("free_context", "strong", "Step exactly matches sanitizer free_context."))
            elif any(line_matches(step, loc) for loc in free_stack_locs):
                grounding.append(evidence("free_context", "strong", "Step appears in sanitizer free_stack."))
            elif any(loc_matches_function_nearby(step, loc) for loc in free_stack_locs):
                grounding.append(evidence("free_context_function", "medium", "Step is in the same sanitizer free-stack function within a small source-line drift window."))
            elif any(loc_matches_file_nearby(step, loc) for loc in free_stack_locs):
                grounding.append(evidence("free_context_nearby", "weak", "Step is in the same file and near a sanitizer free-stack line; function or exact line differs."))

        step_loc = step_loc_or_none(step)
        if step_loc and (loc_set_contains(step_loc, changed_lines) or (
            loc_set_contains(step_loc, hunk_lines) and role in PATCH_GROUNDABLE_ROLES
        )
        ):
            grounding.append(evidence("patch", "medium", "Step is on a changed patch line or a patch hunk line with a patch-groundable role."))
        elif role in PATCH_GROUNDABLE_ROLES and patch_code_matches(step, changed_code):
            grounding.append(evidence("patch", "medium", "Step code matches a removed vulnerable line in patch.diff."))
        elif role in PATCH_GROUNDABLE_ROLES and patch_code_matches(step, hunk_code):
            grounding.append(evidence("patch", "medium", "Step code matches a patch hunk context line in patch.diff."))
        elif step_loc and role in PATCH_GROUNDABLE_ROLES and (
            loc_set_nearby(step_loc, changed_lines) or loc_set_nearby(step_loc, hunk_lines)
        ):
            grounding.append(evidence("patch_nearby", "weak", "Step is near a patch hunk in the same file; exact vulnerable-source line differs."))

        if not grounding:
            grounding.append(evidence("asserted", "weak", "No sanitizer or patch artifact directly grounded this step."))

        # Deduplicate while preserving order.
        seen = set()
        deduped = []
        for item in grounding:
            key = (item["type"], item["strength"], item["evidence"])
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        step["grounding"] = deduped

    update_cross_validation(data)
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ground_truth")
    parser.add_argument("--patch", required=True)
    parser.add_argument(
        "--watchpoint",
        default="",
        help="Accepted for backwards-compatible callers; watchpoint hits are not used by this deterministic grounding pass.",
    )
    parser.add_argument("--in-place", action="store_true")
    args = parser.parse_args()

    gt_path = Path(args.ground_truth)
    data = json.loads(gt_path.read_text())
    data = compute(data, Path(args.patch))
    output = json.dumps(data, indent=2) + "\n"
    if args.in_place:
        gt_path.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
