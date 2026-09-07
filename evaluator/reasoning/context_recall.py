#!/usr/bin/env python3
"""Function-level context recall from saved agent context visits.

`context_gt.json` records the source locations that matter for reproducing a
sample. `context_visit.json` records source files/functions that the agent saw
through checkpointed tool calls. This scorer intentionally measures only the
offline observation record; it does not inspect prompts, submitted PoCs, or
runtime execution logs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"
_SPACE_RE = re.compile(r"\s+")
_FILE_SENTINELS = {"", "<file>", "unknown"}
_RUNTIME_FILE_MARKERS = (
    "llvm-project/compiler-rt/",
    "compiler-rt/lib/fuzzer/",
    "lib/fuzzer/",
    "libfuzzer/",
    "aflplusplus/",
    "sanitizer_common/",
    "/usr/include/",
    "sysdeps/",
)
_RUNTIME_FILE_BASENAMES = {
    "FuzzerDriver.cpp",
    "FuzzerLoop.cpp",
    "FuzzerMain.cpp",
    "aflpp_driver.c",
    "afl_driver.cpp",
    "libc_start_call_main.h",
}
_RUNTIME_FUNCTIONS = {
    "__libc_start_call_main",
    "__libc_start_main",
    "ExecuteCallback",
    "ExecuteFilesOnyByOne",
}


@dataclass(frozen=True)
class ContextPoint:
    file: str
    function: str
    line: int | None
    kind: str


def score_context_recall(
    sample_id: str,
    sample_dir: Path,
    *,
    gt_dir: Path | None = None,
    visit_path: Path | None = None,
) -> dict[str, Any]:
    """Score file and function recall for a sample's visited source context."""
    gt_path = (gt_dir or (GT_RESULTS / sample_id)) / "context_gt.json"
    visit_json_path = visit_path or (sample_dir / "context_visit.json")
    if not gt_path.is_file():
        return _unavailable(sample_id, f"context_gt.json missing: {gt_path}")
    if not visit_json_path.is_file():
        return _unavailable(sample_id, f"context_visit.json missing: {visit_json_path}")

    gt_payload = _load_object(gt_path)
    visit_payload = _load_object(visit_json_path)
    gt_points = _points(gt_payload.get("context"))
    visit_points = _points(visit_payload.get("context"))

    gt_files = _dedupe_files(gt_points)
    visit_files = _dedupe_files(visit_points)
    matched_files = [
        {"file": target, "matched_by": _first_matching_file(target, visit_files)}
        for target in gt_files
    ]

    gt_functions = _dedupe_functions(gt_points)
    visit_functions = _dedupe_functions(visit_points)
    function_reports = [
        _match_function(target, visit_functions)
        for target in gt_functions
    ]
    unmatched_visits = _unmatched_visit_functions(visit_functions, function_reports)

    recoverable = (visit_payload.get("collection") or {}).get("recoverable")
    return {
        "evaluation_protocol": "context-function-recall-v1",
        "sample_id": sample_id,
        "context_visit_recoverable": bool(recoverable),
        "context_visit_path": str(visit_json_path.relative_to(sample_dir)),
        "matching_policy": "file_basename; function_name_and_file_basename; runtime_harness_frames_excluded",
        "files": {
            "total": len(gt_files),
            "covered": sum(1 for item in matched_files if item["matched_by"]),
            "recall": _ratio(
                sum(1 for item in matched_files if item["matched_by"]),
                len(gt_files),
            ),
        },
        "functions": {
            "total": len(gt_functions),
            "covered": sum(1 for item in function_reports if item["matched"]),
            "recall": _ratio(
                sum(1 for item in function_reports if item["matched"]),
                len(gt_functions),
            ),
        },
        "visit_functions_total": len(visit_functions),
        "file_reports": matched_files,
        "function_reports": function_reports,
        "visit_unmatched_count": len(unmatched_visits),
        "visit_unmatched_examples": unmatched_visits[:50],
    }


def _unavailable(sample_id: str, reason: str) -> dict[str, Any]:
    return {
        "evaluation_protocol": "context-function-recall-v1",
        "sample_id": sample_id,
        "unavailable": reason,
        "matching_policy": "file_basename; function_name_and_file_basename; runtime_harness_frames_excluded",
        "files": {"total": 0, "covered": 0, "recall": None},
        "functions": {"total": 0, "covered": 0, "recall": None},
    }


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _points(raw: Any) -> list[ContextPoint]:
    if not isinstance(raw, list):
        return []
    points: list[ContextPoint] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        file = _norm_path(item.get("file"))
        if not file or "*" in file:
            continue
        function = _norm_function(item.get("function"))
        if _is_runtime_context(file, function):
            continue
        points.append(
            ContextPoint(
                file=file,
                function=function,
                line=_int_or_none(item.get("line")),
                kind=str(item.get("kind") or ""),
            )
        )
    return points


def _dedupe_files(points: list[ContextPoint]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for point in points:
        key = _file_basename(point.file)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(point.file)
    return sorted(result, key=lambda item: (_file_basename(item), item))


def _dedupe_functions(points: list[ContextPoint]) -> list[ContextPoint]:
    seen: set[tuple[str, str]] = set()
    result: list[ContextPoint] = []
    for point in points:
        if point.function in _FILE_SENTINELS:
            continue
        key = (_file_basename(point.file), _norm_function(point.function))
        if key in seen:
            continue
        seen.add(key)
        result.append(point)
    return sorted(result, key=lambda item: (_file_basename(item.file), item.function, item.file, item.line or 0))


def _first_matching_file(target: str, visits: list[str]) -> str | None:
    for visit in visits:
        if _path_matches(target, visit):
            return visit
    return None


def _match_function(
    target: ContextPoint,
    visits: list[ContextPoint],
) -> dict[str, Any]:
    for visit in visits:
        if _path_matches(target.file, visit.file) and _function_matches(
            target.function, visit.function
        ):
            return {
                "matched": True,
                "target": _point_dict(target),
                "matched_by": _point_dict(visit),
            }
    return {"matched": False, "target": _point_dict(target), "matched_by": None}


def _unmatched_visit_functions(
    visits: list[ContextPoint],
    reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matched = {
        (item["matched_by"]["file"], item["matched_by"]["function"])
        for item in reports
        if item.get("matched_by")
    }
    return [
        _point_dict(visit)
        for visit in visits
        if (visit.file, visit.function) not in matched
    ]


def _point_dict(point: ContextPoint) -> dict[str, Any]:
    return {
        "file": point.file,
        "file_basename": _file_basename(point.file),
        "function": point.function,
        "line": point.line,
        "kind": point.kind,
    }


def _path_matches(left: str, right: str) -> bool:
    basename = _file_basename(left)
    return bool(basename) and basename == _file_basename(right)


def _is_runtime_context(file: str, function: str) -> bool:
    lowered = _norm_path(file).lower()
    basename = _file_basename(file)
    if basename in _RUNTIME_FILE_BASENAMES:
        return True
    if any(marker in lowered for marker in _RUNTIME_FILE_MARKERS):
        return True
    return _norm_function(function) in _RUNTIME_FUNCTIONS


def _file_basename(value: Any) -> str:
    return _norm_path(value).rsplit("/", 1)[-1]


def _norm_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    for prefix in (
        "/workspace/repo-vul/src-vul/",
        "/workspace/repo-vul/",
        "/workspace/",
        "/gt/_work/src/",
        "/gt/_work/",
    ):
        if text.startswith(prefix):
            text = text[len(prefix):]
    parts = [part for part in text.split("/") if part and part not in {".", "src-vul"}]
    return "/".join(parts)


def _function_matches(left: str, right: str) -> bool:
    l, r = _norm_function(left), _norm_function(right)
    if not l or not r:
        return False
    if l == r:
        return True
    if "::" in l and "::" in r:
        return False
    qualified, plain = (l, r) if "::" in l else (r, l)
    return qualified.split("::")[-1] == plain


def _norm_function(value: Any) -> str:
    return _SPACE_RE.sub("", str(value or "").strip())



def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None
