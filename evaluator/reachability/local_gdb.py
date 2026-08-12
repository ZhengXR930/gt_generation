"""Execute a local-workspace RuntimeSpec under deterministic GDB checkpoints."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from reachability.engine import CommandResult, load_hits, write_breakpoint_spec
from reachability.runtime_spec import RuntimeSpec, container_path_on_host


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

    executable = spec.executable
    # Relative executables are intentionally retained relative to the exact
    # recorded container workdir; validation already proved the mapped file exists.
    container_path_on_host(gt_dir, executable, spec.workdir)
    candidate = str(poc_path.resolve())
    if not _is_relative_to(Path(candidate), repo_root.resolve()):
        raise RuntimeError("PoC path must be inside the mounted repository")
    arguments = [item.replace(spec.input_placeholder, candidate) for item in spec.arguments]
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
        executable, *arguments,
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
