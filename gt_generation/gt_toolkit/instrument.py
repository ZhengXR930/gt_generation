"""GDB watchpoint / breakpoint coverage recorder.

Earlier role prompts referenced a `gdb_watch.py` script that never existed.
This subcommand ships the recorder as bundled package data (gdb_recorder.py) and
either prints the exact GDB invocation or runs it, so watchpoint coverage is
reproducible from any CLI.

The recorder itself (gdb_recorder.py) runs *inside* GDB's Python interpreter.
It reads its config from environment variables and writes a structured JSON
artifact of {kind, var, file, line, function, backtrace} hits for the
deterministic matcher layer to consume.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

RECORDER = Path(__file__).resolve().parent / "gdb_recorder.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gt-toolkit gdb-watch",
        description="Emit or run the GDB watchpoint/breakpoint recorder.",
    )
    parser.add_argument("--binary", help="Debug binary (-O0 -g, no sanitizer).")
    parser.add_argument("--args", default="", help="Arguments passed to the binary (use {poc}).")
    parser.add_argument("--poc", default="", help="PoC path substituted for {poc}.")
    parser.add_argument("--watch", action="append", default=[], help="Watch expression (repeatable).")
    parser.add_argument("--break", dest="breaks", action="append", default=[], help="Breakpoint file:line (repeatable).")
    parser.add_argument("--start-commands", default="", help="Semicolon GDB commands run before watches (e.g. 'break f;run').")
    parser.add_argument("--out", type=Path, default=Path("watchpoint.json"))
    parser.add_argument("--run", action="store_true", help="Execute GDB instead of only printing the command.")
    args = parser.parse_args(argv)

    prog_args = args.args.replace("{poc}", args.poc) if args.poc else args.args

    env = dict(os.environ)
    env["GDB_WATCH_EXPRESSIONS"] = json.dumps(args.watch)
    env["GDB_BREAKPOINTS"] = json.dumps(args.breaks)
    env["GDB_WATCH_OUTPUT"] = str(args.out)
    if args.start_commands:
        env["GDB_START_COMMANDS"] = args.start_commands

    if not args.binary:
        # Nothing to run against; just show the recorder path and env contract.
        print(json.dumps({"recorder": str(RECORDER), "env": {k: env[k] for k in
              ("GDB_WATCH_EXPRESSIONS", "GDB_BREAKPOINTS", "GDB_WATCH_OUTPUT")}}, indent=2))
        return 0

    cmd = ["gdb", "--batch", "-x", str(RECORDER), "--args", args.binary, *prog_args.split()]
    printable = " ".join(
        f"{k}={env[k]!r}" for k in ("GDB_WATCH_EXPRESSIONS", "GDB_BREAKPOINTS", "GDB_WATCH_OUTPUT")
    ) + " " + " ".join(cmd)

    if not args.run:
        print(printable)
        return 0

    proc = subprocess.run(cmd, env=env)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
