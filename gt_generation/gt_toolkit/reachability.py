"""Thin, portable wrapper over the repo's reachability engine.

Roles call `python3 -m reachability.cli`, which resolves with `evaluation_mode/`
on PYTHONPATH. This wrapper finds it relative to the repo root and delegates, so
any coding-agent CLI can just call
`python3 -m gt_toolkit reachability ...` from anywhere in the tree.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    """Find the repo root by walking up until the evaluation engine is present.

    gt_toolkit/ lives at gt_generation/gt_toolkit/, while the reachability engine
    evaluation_mode/ lives at the actual repo root one level above, so
    a fixed parents[] index is fragile — search instead.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "evaluation_mode" / "reachability").exists():
            return parent
    # Fall back to the repo root (parent of gt_generation/).
    return here.parents[2]


def _engine_env(root: Path) -> dict[str, str]:
    eval_dir = root / "evaluation_mode"
    missing = [str(eval_dir)] if not (eval_dir / "reachability").exists() else []
    if missing:
        raise FileNotFoundError(
            "reachability engine not found; expected evaluation_mode/reachability under "
            f"{root} (missing: {', '.join(missing)})"
        )
    env = dict(os.environ)
    extra = str(eval_dir)
    env["PYTHONPATH"] = extra + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gt-toolkit reachability",
        description="Run R1-R5 PoC reachability against a ground_truth.json.",
    )
    parser.add_argument("--gt", required=True, type=Path)
    parser.add_argument("--poc", type=Path)
    parser.add_argument("--debug-command", help="Debug command with optional {poc} placeholder.")
    parser.add_argument("--sanitizer-command", help="Sanitizer command with optional {poc} placeholder.")
    parser.add_argument("--sanitizer-trace", type=Path)
    parser.add_argument("--codebase", type=Path)
    parser.add_argument("--assertion-spec", type=Path)
    parser.add_argument("--assertion-trace", type=Path)
    parser.add_argument("--verified-invariants", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("reachability_out"))
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)

    root = _repo_root()
    try:
        env = _engine_env(root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cmd = [sys.executable, "-m", "reachability.cli", "--gt", str(args.gt)]
    for flag, value in [
        ("--poc", args.poc),
        ("--debug-command", args.debug_command),
        ("--sanitizer-command", args.sanitizer_command),
        ("--sanitizer-trace", args.sanitizer_trace),
        ("--codebase", args.codebase),
        ("--assertion-spec", args.assertion_spec),
        ("--assertion-trace", args.assertion_trace),
        ("--verified-invariants", args.verified_invariants),
        ("--out-dir", args.out_dir),
        ("--timeout", args.timeout),
    ]:
        if value is not None:
            cmd += [flag, str(value)]

    proc = subprocess.run(cmd, env=env, cwd=str(root))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
