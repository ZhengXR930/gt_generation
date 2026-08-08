"""gt-toolkit: portable, CLI-agnostic deterministic tools for GT generation.

Subcommands:

  validate       Validate a ground_truth.json (schema + tiered quality checks).
  state          Manage the canonical sample_state.json.
  reachability   Run R1-R5 PoC reachability against a ground_truth.json.
  assertions     Verify frozen assertions and summarize perturbation witnesses.
  assertion-preflight Reject invalid assertion plans before instrumentation.
  audit-package  Validate a completed GT result directory and all evidence references.
  bind-evidence  Commit the final GT and evidence files by content hash.
  compact-result Remove generation-only files from a validated result directory.
  arvo-workspace Reuse one ARVO container across vulnerable/fixed validation.
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
    commands = [
        "prepare", "validate", "state", "reachability", "assertions",
        "assertion-preflight", "audit-package", "bind-evidence",
        "compact-result", "arvo-workspace",
        "gdb-watch", "schema-path",
    ]
    parser = argparse.ArgumentParser(prog="gt-toolkit", description=__doc__)
    parser.add_argument("--version", action="version", version=f"gt-toolkit {__version__}")
    parser.add_argument(
        "command",
        choices=commands,
        help="Subcommand to run.",
    )
    # Once a command is selected, every following option belongs to that command.
    # This prevents a subcommand option such as `arvo-workspace run --version fixed`
    # from being consumed by the top-level `gt-toolkit --version` flag.
    if argv and argv[0] in commands:
        ns = argparse.Namespace(command=argv[0])
        rest = argv[1:]
    else:
        ns, rest = parser.parse_known_args(argv)

    if ns.command == "prepare":
        from . import prepare
        return prepare.main(rest)
    if ns.command == "validate":
        from . import validate
        return validate.main(rest)
    if ns.command == "state":
        from . import state
        return state.main(rest)
    if ns.command == "reachability":
        from . import reachability
        return reachability.main(rest)
    if ns.command == "assertions":
        from . import assertions
        return assertions.main(rest)
    if ns.command == "assertion-preflight":
        from . import assertion_preflight
        return assertion_preflight.main(rest)
    if ns.command == "audit-package":
        from . import package_audit
        return package_audit.main(rest)
    if ns.command == "bind-evidence":
        from . import evidence
        return evidence.main(rest)
    if ns.command == "compact-result":
        from . import compact_result
        return compact_result.main(rest)
    if ns.command == "arvo-workspace":
        from . import arvo_workspace
        return arvo_workspace.main(rest)
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
