#!/usr/bin/env python3
"""Lightweight, debugger-free reachability via libFuzzer coverage.

Replaces the gdb/ptrace engine for evaluating how far a subject PoC reaches. The
ARVO/OSS-Fuzz target binary is built with SanitizerCoverage, so running it once
with `-print_coverage=1 -runs=0` prints every reached line as

    COVERED: in <function> <file>:<line>

We parse that into the set of reached functions and (file, line) pairs, then map
the GT reachability checkpoints onto it to produce `hits` in the exact shape the
gdb engine emitted -- so evaluate_r1_r5() consumes them unchanged. This needs no
debugger, no ptrace, and no code changes to the binary, so it runs fine under
qemu emulation (e.g. an ARVO amd64 image on an Apple-Silicon host), where gdb
cannot read registers at all.

R1 format admission, R2 source, and R4 vulnerable-line checkpoints require the
exact GT (file, line). R3 function checkpoints use function-level coverage.
This keeps format admission tied to the per-sample GT boundary instead of
treating entry into a generic fuzz target or parser function as acceptance.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

_COVERED_RE = re.compile(r"^COVERED: in (\S+) (.+):(\d+)\s*$")
_EXACT_LINE_KINDS = {
    "parser_admitted",
    "source",
    "root_cause_line",
    "sink_line",
}


def _run_coverage_in_image(image: str, poc_path: Path, timeout: int) -> str:
    """One container run: detect the fuzz target from /bin/arvo, execute the PoC
    once with libFuzzer coverage printing, return the raw COVERED lines."""
    script = (
        'T=$(grep -oE "/out/[A-Za-z0-9_]+" /bin/arvo | head -1); '
        '[ -z "$T" ] && { echo NO_TARGET; exit 3; }; '
        '"$T" -print_coverage=1 -runs=0 /tmp/poc 2>&1 | grep "^COVERED: in "'
    )
    proc = subprocess.run(
        [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "-e", "LANG=C.UTF-8",
            "-v", f"{poc_path.resolve()}:/tmp/poc:ro",
            "--entrypoint", "/bin/bash", image, "-lc", script,
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.stdout


def parse_covered(text: str) -> tuple[set[str], set[tuple[str, int]]]:
    """(reached function names, reached (file-basename, line) pairs)."""
    functions: set[str] = set()
    lines: set[tuple[str, int]] = set()
    for raw in text.splitlines():
        m = _COVERED_RE.match(raw.strip())
        if not m:
            continue
        func, path, line = m.group(1), m.group(2), int(m.group(3))
        functions.add(func)
        lines.add((os.path.basename(path), line))
    return functions, lines


def checkpoints_to_hits(
    checkpoints: list[dict[str, Any]],
    functions: set[str],
    lines: set[tuple[str, int]],
) -> list[dict[str, Any]]:
    """Emit a gdb-engine-shaped hit for every checkpoint the coverage shows was
    reached, so evaluate_r1_r5() can score it unchanged."""
    hits = []
    for cp in checkpoints:
        kind = str(cp.get("kind") or "")
        fn = str(cp.get("function") or "")
        fbase = os.path.basename(str(cp.get("file") or ""))
        line = cp.get("line")
        if kind in _EXACT_LINE_KINDS:
            reached = isinstance(line, int) and (fbase, line) in lines
        else:
            reached = bool(fn) and fn in functions
        if reached:
            hits.append({
                "kind": kind,
                "function": fn,
                "file": cp.get("file"),
                "line": line,
            })
    return hits


def coverage_hits(
    *, image: str, poc_path: Path, checkpoints: list[dict[str, Any]], timeout: int = 300
) -> list[dict[str, Any]] | None:
    """Run the PoC in `image` with coverage printing and return reachability hits
    for `checkpoints`. ``None`` means coverage was unavailable; an empty list
    means coverage ran successfully but none of the GT checkpoints were hit."""
    text = _run_coverage_in_image(image, poc_path, timeout)
    functions, lines = parse_covered(text)
    if not functions:
        return None
    return checkpoints_to_hits(checkpoints, functions, lines)
