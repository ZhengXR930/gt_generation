"""Thin, portable wrapper over the repo's reachability engine.

Roles call `python3 -m reachability.cli`, which resolves with `evaluator/`
on PYTHONPATH. This wrapper finds it relative to the repo root and delegates, so
any coding-agent CLI can just call
`python3 -m gt_toolkit reachability ...` from anywhere in the tree.
"""

from __future__ import annotations

import argparse
import shlex
import re
import json
import os
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    """Find the repo root by walking up until the evaluation engine is present.

    gt_toolkit/ lives at gt_generation/gt_toolkit/, while the reachability engine
    evaluator/ lives at the actual repo root one level above, so
    a fixed parents[] index is fragile — search instead.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "evaluator" / "reachability").exists():
            return parent
    # Fall back to the repo root (parent of gt_generation/).
    return here.parents[2]


def _engine_env(root: Path) -> dict[str, str]:
    eval_dir = root / "evaluator"
    missing = [str(eval_dir)] if not (eval_dir / "reachability").exists() else []
    if missing:
        raise FileNotFoundError(
            "reachability engine not found; expected evaluator/reachability under "
            f"{root} (missing: {', '.join(missing)})"
        )
    env = dict(os.environ)
    extra = str(eval_dir)
    env["PYTHONPATH"] = extra + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def derive_debug_command(result_dir: Path) -> str | None:
    """Recover the bare program and arguments Stage 01 used to reproduce.

    The recorded command carries sanitizer environment assignments and may be
    wrapped in build.sh; gdb --args wants neither.
    """
    report = result_dir / "reproduction_report.json"
    if not report.is_file():
        return None
    try:
        command = str(json.loads(report.read_text(encoding="utf-8")).get("command") or "")
        parts = shlex.split(command)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if parts and parts[0].endswith("build.sh") and len(parts) >= 2:
        try:
            parts = shlex.split(parts[1])
        except ValueError:
            return None
    while parts and _ENV_ASSIGNMENT.match(parts[0]):
        parts.pop(0)
    if not parts:
        return None
    poc_names = {"/gt/poc", "poc", "./poc", str(result_dir / "poc")}
    argv = ["{poc}" if part in poc_names else part for part in parts]
    if "{poc}" not in argv:
        argv.append("{poc}")
    return " ".join(argv)


def run_for_result_dir(result_dir: Path, timeout: int = 900) -> int:
    """Run reachability for one package, on whichever side of the wall it lives.

    Repo-track packages execute through their own build.sh so the tool, the
    target and gdb share one filesystem; ARVO keeps the direct invocation the
    agent already builds around its workspace container.
    """
    result_dir = result_dir.resolve()
    build_sh = result_dir / "build.sh"
    if not build_sh.is_file():
        print(json.dumps({"reachability": "skipped", "reason": "no build.sh"}))
        return 0
    if "gt-memory-env" not in build_sh.read_text(encoding="utf-8", errors="replace"):
        print(json.dumps({"reachability": "skipped", "reason": "arvo track"}))
        return 0
    debug_command = derive_debug_command(result_dir)
    if not debug_command:
        print(json.dumps({"reachability": "skipped", "reason": "no reproduction command"}))
        return 0

    inner = " ".join([
        "PYTHONPATH=/repo/gt_generation:/repo",
        "python3", "-m", "gt_toolkit", "reachability",
        "--gt", "/gt/ground_truth.json",
        "--codebase", "/gt/_work/src",
        "--assertion-spec", "/gt/candidate_assertions.json",
        "--assertion-trace", "/gt/vulnerable_assertion_trace.txt",
        "--verified-invariants", "/gt/verified_invariants.json",
        "--sanitizer-trace", "/gt/sanitizer_trace.txt",
        "--debug-command", shlex.quote(debug_command),
        "--poc", "/gt/poc",
        "--out-dir", "/gt/reachability",
    ])
    proc = subprocess.run(
        [str(build_sh), inner], capture_output=True, text=True,
        errors="replace", timeout=timeout,
    )
    produced = result_dir / "reachability" / "reachability_report.json"
    if produced.is_file():
        # audit-package reads the report at the package root.
        (result_dir / "reachability_report.json").write_text(
            produced.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(json.dumps({"reachability": "ran", "returncode": proc.returncode}))
        return 0
    print(json.dumps({
        "reachability": "failed",
        "returncode": proc.returncode,
        "stderr": proc.stderr[-600:],
    }))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gt-toolkit reachability",
        description="Run R1-R5 PoC reachability against a ground_truth.json.",
    )
    parser.add_argument("--gt", type=Path)
    parser.add_argument("--poc", type=Path)
    parser.add_argument("--debug-command", help="Debug command with optional {poc} placeholder.")
    parser.add_argument(
        "--debug-wrapper",
        help="Run gdb through this command (e.g. the sample build.sh) as one shell word.",
    )
    parser.add_argument(
        "--debug-path-map",
        help="HOST=CONTAINER prefix rewrite for paths passed to --debug-wrapper.",
    )
    parser.add_argument("--sanitizer-command", help="Sanitizer command with optional {poc} placeholder.")
    parser.add_argument("--sanitizer-trace", type=Path)
    parser.add_argument("--codebase", type=Path)
    parser.add_argument("--assertion-spec", type=Path)
    parser.add_argument("--assertion-trace", type=Path)
    parser.add_argument("--verified-invariants", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("reachability_out"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--for-result-dir",
        type=Path,
        help=(
            "Run reachability for this package without an agent: recover the "
            "reproduction command and, on the repo track, execute the tool "
            "inside the sample image through its build.sh."
        ),
    )
    args = parser.parse_args(argv)
    if not args.for_result_dir and not args.gt:
        parser.error('--gt is required unless --for-result-dir is given')
    if args.for_result_dir:
        return run_for_result_dir(args.for_result_dir, timeout=max(args.timeout, 900))

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
        ("--debug-wrapper", args.debug_wrapper),
        ("--debug-path-map", args.debug_path_map),
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
