"""GDB Python recorder. Runs INSIDE gdb: `gdb --batch -x gdb_recorder.py --args <bin> <args>`.

Config comes from environment variables (set by `gt-toolkit gdb-watch`):

  GDB_WATCH_EXPRESSIONS  JSON list of watch expressions, e.g. ["mp4config.frame.ents"]
  GDB_BREAKPOINTS        JSON list of "file:line" breakpoints, e.g. ["frontend/mp4read.c:355"]
  GDB_START_COMMANDS     Optional ';'-separated gdb commands run before watches are set
                         (use when a local watch expr is only valid after stopping in a frame)
  GDB_WATCH_OUTPUT       Output JSON path (default watchpoint.json)

Output: a JSON list of hit records {kind, var, file, line, function, backtrace}
for the deterministic matcher layer. This file is plain data; validators must
not parse human-readable gdb transcripts.
"""

from __future__ import annotations

import json
import os

try:
    import gdb  # type: ignore  # provided by the gdb python runtime
except ImportError:  # pragma: no cover - only importable inside gdb
    gdb = None

_HITS: list[dict] = []


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]
    except json.JSONDecodeError:
        return [raw]


def _frame_location() -> dict:
    try:
        frame = gdb.selected_frame()
        sal = frame.find_sal()
        return {
            "function": frame.name() or "",
            "file": (sal.symtab.filename if sal and sal.symtab else "") or "",
            "line": int(sal.line) if sal and sal.line else None,
        }
    except Exception:
        return {"function": "", "file": "", "line": None}


def _backtrace(limit: int = 12) -> list[str]:
    frames: list[str] = []
    try:
        frame = gdb.newest_frame()
        while frame is not None and len(frames) < limit:
            sal = frame.find_sal()
            loc = f"{sal.symtab.filename}:{sal.line}" if sal and sal.symtab else "?"
            frames.append(f"{frame.name() or '??'} @ {loc}")
            frame = frame.older()
    except Exception:
        pass
    return frames


class _Recorder(gdb.Breakpoint if gdb else object):  # type: ignore
    def __init__(self, spec: str, kind: str, var: str = ""):
        if kind == "watch":
            super().__init__(spec, gdb.BP_WATCHPOINT, gdb.WP_WRITE, internal=False)
        else:
            super().__init__(spec)
        self._kind = kind
        self._var = var or spec

    def stop(self) -> bool:  # noqa: D401 - gdb callback
        loc = _frame_location()
        _HITS.append({
            "kind": self._kind,
            "var": self._var,
            "file": loc["file"],
            "line": loc["line"],
            "function": loc["function"],
            "backtrace": _backtrace(),
        })
        return False  # keep going; we only record


def _run() -> None:
    gdb.execute("set pagination off")
    gdb.execute("set confirm off")

    for cmd in [c for c in os.environ.get("GDB_START_COMMANDS", "").split(";") if c.strip()]:
        try:
            gdb.execute(cmd.strip())
        except gdb.error:
            pass

    for bp in _env_list("GDB_BREAKPOINTS"):
        try:
            _Recorder(bp, "breakpoint")
        except Exception:
            pass
    for expr in _env_list("GDB_WATCH_EXPRESSIONS"):
        try:
            _Recorder(expr, "watch", var=expr)
        except Exception:
            pass

    try:
        gdb.execute("run")
    except gdb.error:
        pass
    try:
        while True:
            gdb.execute("continue")
    except gdb.error:
        pass

    out = os.environ.get("GDB_WATCH_OUTPUT", "watchpoint.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(_HITS, handle, indent=2, ensure_ascii=False)


if gdb is not None:
    _run()
