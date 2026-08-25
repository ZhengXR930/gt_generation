"""Execute a local-workspace RuntimeSpec under deterministic GDB checkpoints."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from evaluator.reachability.engine import CommandResult, load_hits, write_breakpoint_spec
from evaluator.reachability.runtime_spec import (
    RuntimeSpec,
    RuntimeSpecError,
    container_path_on_host,
    ensure_source_workspace,
)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def run_local_gdb(
    *,
    spec: RuntimeSpec,
    gt_dir: Path,
    poc_path: Path,
    checkpoints: list[dict],
    output_dir: Path,
    repo_root: Path,
    timeout: int,
    max_hits_per_event: int = 64,
) -> tuple[CommandResult, list[dict], bool]:
    output_dir.mkdir(parents=True, exist_ok=True)
    breakpoints_path = output_dir / "reachability_breakpoints.json"
    hits_path = output_dir / "reachability_hits.json"
    write_breakpoint_spec(checkpoints, breakpoints_path)
    try:
        hits_path.unlink()
    except FileNotFoundError:
        pass

    _ensure_runtime_prepared(
        spec=spec,
        gt_dir=gt_dir,
        repo_root=repo_root,
        output_dir=output_dir,
        timeout=max(timeout, 1800),
    )
    executable = spec.executable
    # Relative executables are intentionally retained relative to the exact
    # recorded container workdir; validation already proved the mapped file exists.
    executable_host = container_path_on_host(gt_dir, executable, spec.workdir)
    candidate = str(poc_path.resolve())
    if not _is_relative_to(Path(candidate), repo_root.resolve()):
        raise RuntimeError("PoC path must be inside the mounted repository")
    arguments = [item.replace(spec.input_placeholder, candidate) for item in spec.arguments]
    gdb_executable, gdb_arguments = _gdb_invocation_for_runtime(
        executable=executable,
        executable_host=executable_host,
        arguments=arguments,
    )
    gdb_script = repo_root / "evaluator" / "reachability" / "gdb_reachability.py"
    command = [
        "docker", "run", "--rm", "--platform", "linux/amd64",
        "--cap-add", "SYS_PTRACE", "--security-opt", "seccomp=unconfined",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp",
        "-e", f"REACHABILITY_BREAKPOINTS={breakpoints_path}",
        "-e", f"REACHABILITY_OUTPUT={hits_path}",
        "-e", f"REACHABILITY_MAX_HITS_PER_BREAKPOINT={max_hits_per_event}",
    ]
    for key, value in sorted(spec.environment.items()):
        command.extend(["-e", f"{key}={value}"])
    command.extend([
        "-v", f"{repo_root}:{repo_root}",
        "-v", f"{gt_dir.resolve()}:/gt",
        "-w", spec.workdir,
        spec.image,
        "gdb", "--batch", "-q", "-x", str(gdb_script), "--args",
        gdb_executable, *gdb_arguments,
    ])
    try:
        proc = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        result = CommandResult(command, proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = CommandResult(command, 124, stdout, stderr + "\nexecution timed out\n")
    (output_dir / "gdb_stdout.txt").write_text(result.stdout, encoding="utf-8")
    (output_dir / "gdb_stderr.txt").write_text(result.stderr, encoding="utf-8")
    (output_dir / "gdb_command.json").write_text(
        json.dumps({"command": command, "returncode": result.returncode}, indent=2) + "\n",
        encoding="utf-8",
    )
    hits = load_hits(hits_path) if hits_path.is_file() else []
    checked = (
        result.returncode == 0
        and hits_path.is_file()
        and not any(hit.get("run_error") for hit in hits)
    )
    return result, hits, checked


def _gdb_invocation_for_runtime(
    *, executable: str, executable_host: Path, arguments: list[str]
) -> tuple[str, list[str]]:
    """Return the program argv GDB should launch for a RuntimeSpec.

    Runtime validation is allowed to use a small shell wrapper, because it only
    needs to execute the candidate.  Reachability needs debug symbols from the
    real target binary.  For generated non-ARVO specs the wrapper usually ends
    in `exec "$target" ...`; launching it as `/bin/bash wrapper ...` lets GDB
    follow the fork/exec into that target while keeping pending source
    breakpoints.
    """
    try:
        with executable_host.open("rb") as handle:
            is_script = handle.read(2) == b"#!"
    except OSError:
        is_script = False
    if not is_script:
        return executable, arguments
    return "/bin/bash", [executable, *arguments]


def _ensure_runtime_prepared(
    *,
    spec: RuntimeSpec,
    gt_dir: Path,
    repo_root: Path,
    output_dir: Path,
    timeout: int,
) -> None:
    if spec.backend != "local_workspace":
        return
    ensure_source_workspace(gt_dir, spec, timeout=timeout)
    executable = container_path_on_host(gt_dir, spec.executable, spec.workdir)
    if executable.is_file() and executable.stat().st_mode & 0o111:
        return
    if not spec.build_commands:
        raise RuntimeSpecError(f"runtime executable is missing: {executable}")
    build_script = "set -e\n" + "\n".join(spec.build_commands)
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
        "-v",
        f"{repo_root}:{repo_root}:ro",
        "-v",
        f"{gt_dir.resolve()}:/gt",
        "-w",
        spec.build_workdir,
        spec.image,
        "bash",
        "-lc",
        build_script,
    ]
    proc = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    (output_dir / "runtime_build_stdout.txt").write_text(
        proc.stdout, encoding="utf-8"
    )
    (output_dir / "runtime_build_stderr.txt").write_text(
        proc.stderr, encoding="utf-8"
    )
    (output_dir / "runtime_build_command.json").write_text(
        json.dumps({"command": command, "returncode": proc.returncode}, indent=2) + "\n",
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeSpecError(f"runtime build failed with exit code {proc.returncode}")
    if not executable.is_file():
        raise RuntimeSpecError(f"runtime build did not create executable: {executable}")
    if not executable.stat().st_mode & 0o111:
        raise RuntimeSpecError(f"runtime executable is not executable: {executable}")
