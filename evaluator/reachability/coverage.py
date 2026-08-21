"""Coverage-ledger helpers for reachability evaluation."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def checkpoints_to_hits(
    checkpoints: list[dict[str, Any]],
    *,
    functions: set[str] | None = None,
    lines: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Convert sampled function/line coverage into reachability hit records.

    Line checkpoints are intentionally exact: parser admission, source, root
    line, and sink line should only count when the recorded source line was hit.
    Function checkpoints are weaker fallbacks and are matched only by function
    name.
    """
    functions = functions or set()
    lines = lines or set()
    hits: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        kind = str(checkpoint.get("kind") or "")
        line = _to_int(checkpoint.get("line"))
        file = str(checkpoint.get("file") or "")
        function = str(checkpoint.get("function") or "")
        if line is None:
            if function and function in functions:
                hits.append(_hit(checkpoint, file=file, function=function, line=line))
            continue
        if _line_hit(file, line, lines):
            hits.append(_hit(checkpoint, file=file, function=function, line=line))
            continue
        # Only explicit function-level checkpoints may fall back to function
        # coverage. A line checkpoint with the same function is still a miss.
        if kind.endswith("_function") and function and function in functions:
            hits.append(_hit(checkpoint, file=file, function=function, line=line))
    return hits


def _hit(
    checkpoint: dict[str, Any],
    *,
    file: str,
    function: str,
    line: int | None,
) -> dict[str, Any]:
    return {
        "kind": checkpoint.get("kind"),
        "event_point": checkpoint.get("event_point"),
        "assertion_role": checkpoint.get("assertion_role"),
        "expected_file": checkpoint.get("file"),
        "expected_function": checkpoint.get("function"),
        "expected_line": checkpoint.get("line"),
        "file": file,
        "function": function,
        "line": line,
    }


def _line_hit(file: str, line: int, lines: set[tuple[str, int]]) -> bool:
    normalized = _normalize_file(file)
    basename = Path(normalized).name
    return any(
        candidate_line == line
        and (
            _normalize_file(candidate_file).endswith(normalized)
            or normalized.endswith(_normalize_file(candidate_file))
            or Path(_normalize_file(candidate_file)).name == basename
        )
        for candidate_file, candidate_line in lines
    )


def _normalize_file(file: str) -> str:
    file = file.replace("\\", "/").strip()
    if "@" in file:
        file = file.split("@", 1)[0]
    return file.strip("/")


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
