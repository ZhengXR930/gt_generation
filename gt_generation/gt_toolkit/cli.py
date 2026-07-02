"""gt-toolkit: portable, CLI-agnostic deterministic tools for GT generation.

Subcommands:

  validate       Validate a ground_truth.json (schema + tiered quality checks).
  state          Manage the canonical sample_state.json.
  reachability   Run R1-R5 PoC reachability against a ground_truth.json.
  gdb-watch      Emit / run the GDB python recorder for watchpoint coverage.
  schema-path    Print the path to the canonical ground_truth schema.

Every subcommand is runnable without installation:  python3 -m gt_toolkit <cmd>
After `pip install`, the same commands are available as:  gt-toolkit <cmd>
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="gt-toolkit", description=__doc__)
    parser.add_argument("--version", action="version", version=f"gt-toolkit {__version__}")
    parser.add_argument(
        "command",
        choices=["validate", "state", "reachability", "gdb-watch", "schema-path"],
        help="Subcommand to run.",
    )
    ns, rest = parser.parse_known_args(argv)

    if ns.command == "validate":
        from . import validate
        return validate.main(rest)
    if ns.command == "state":
        from . import state
        return state.main(rest)
    if ns.command == "reachability":
        from . import reachability
        return reachability.main(rest)
    if ns.command == "gdb-watch":
        from . import instrument
        return instrument.main(rest)
    if ns.command == "schema-path":
        from .validate import SCHEMA_PATH
        print(SCHEMA_PATH)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
