"""Experiment-local ARVO GDB runner with harness-aware target arguments."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from reachability.arvo_gdb import PreparedTarget
from reachability.engine import CommandResult, load_hits, write_breakpoint_spec

_AFL_MARKER = b"This binary is built for AFL-fuzz"


def runtime_checked(returncode: int, hits: list[dict]) -> bool:
    """A written hit file is not success when GDB could not start the target."""
    return returncode == 0 and not any(hit.get("run_error") for hit in hits)


def target_arguments(executable: Path, poc_path: Path) -> list[str]:
    """AFL targets take a file directly; libFuzzer targets accept `-runs=0`."""
    try:
        is_afl = _AFL_MARKER in executable.read_bytes()
    except OSError:
        is_afl = False
    args = [str(executable)]
    if not is_afl:
        args.append("-runs=0")
    args.append(str(poc_path.resolve()))
    return args


def run_hypothesis_gdb(
    *,
    prepared: PreparedTarget,
    poc_path: Path,
    checkpoints: list[dict],
    output_dir: Path,
    repo_root: Path,
    timeout: int,
    debugger_image: str = "gt-memory-env:latest",
    max_hits_per_breakpoint: int = 1,
) -> tuple[CommandResult, list[dict], bool]:
    output_dir.mkdir(parents=True, exist_ok=True)
    breakpoints_path = output_dir / "reachability_breakpoints.json"
    hits_path = output_dir / "reachability_hits.json"
    write_breakpoint_spec(checkpoints, breakpoints_path)
    hits_path.unlink(missing_ok=True)

    gdb_script = Path(__file__).resolve().with_name("gdb_reachability.py")
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/tmp",
        "-e",
        "ASAN_OPTIONS=detect_leaks=0",
        "-e",
        f"LD_LIBRARY_PATH={prepared.executable.parent}",
        "-e",
        f"REACHABILITY_BREAKPOINTS={breakpoints_path}",
        "-e",
        f"REACHABILITY_OUTPUT={hits_path}",
        "-e",
        f"REACHABILITY_MAX_HITS_PER_BREAKPOINT={max_hits_per_breakpoint}",
        "-v",
        f"{repo_root}:{repo_root}",
        "-v",
        f"{prepared.root}:{prepared.root}:ro",
        "-w",
        str(repo_root),
        debugger_image,
        "gdb",
        "--batch",
        "-q",
        "-x",
        str(gdb_script),
        "--args",
        *target_arguments(prepared.executable, poc_path),
    ]
    proc = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    result = CommandResult(
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    (output_dir / "gdb_stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (output_dir / "gdb_stderr.txt").write_text(proc.stderr, encoding="utf-8")
    (output_dir / "gdb_command.json").write_text(
        json.dumps(
            {"command": command, "returncode": proc.returncode},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    hits = load_hits(hits_path) if hits_path.is_file() else []
    checked = hits_path.is_file() and runtime_checked(proc.returncode, hits)
    return result, hits, checked
