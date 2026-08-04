"""Run exact GT reachability breakpoints for one ARVO target under GDB."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from reachability.engine import CommandResult, load_hits, write_breakpoint_spec

_TARGET_RE = re.compile(rb"/out/([^\s'\"]+)")


@dataclass(frozen=True)
class PreparedTarget:
    root: Path
    executable: Path


def _run(
    command: list[str], *, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


@contextmanager
def prepare_arvo_target(image: str) -> Iterator[PreparedTarget]:
    """Extract `/out` once so every candidate for the sample uses one binary."""
    root = Path(tempfile.mkdtemp(prefix="reachability_arvo_"))
    container_id = ""
    try:
        created = _run(["docker", "create", "--platform", "linux/amd64", image])
        if created.returncode != 0:
            raise RuntimeError(
                f"docker create failed for {image}: {created.stderr.strip()}"
            )
        container_id = created.stdout.strip()
        out_dir = root / "out"
        out_dir.mkdir()
        copied_out = _run(["docker", "cp", f"{container_id}:/out/.", str(out_dir)])
        if copied_out.returncode != 0:
            raise RuntimeError(
                f"could not extract {image}:/out: {copied_out.stderr.strip()}"
            )
        arvo_path = root / "arvo"
        copied_arvo = _run(["docker", "cp", f"{container_id}:/bin/arvo", str(arvo_path)])
        if copied_arvo.returncode != 0:
            raise RuntimeError(
                f"could not extract {image}:/bin/arvo: "
                f"{copied_arvo.stderr.strip()}"
            )
        match = _TARGET_RE.search(arvo_path.read_bytes())
        if not match:
            raise RuntimeError(f"could not identify fuzz target in {image}:/bin/arvo")
        executable = out_dir / match.group(1).decode("ascii")
        if not executable.is_file():
            raise RuntimeError(f"identified target is missing: {executable.name}")
        executable.chmod(executable.stat().st_mode | 0o100)
        yield PreparedTarget(root=root, executable=executable)
    finally:
        if container_id:
            _run(["docker", "rm", "-f", container_id])
        shutil.rmtree(root, ignore_errors=True)


def run_arvo_gdb(
    *,
    prepared: PreparedTarget,
    poc_path: Path,
    checkpoints: list[dict],
    output_dir: Path,
    repo_root: Path,
    timeout: int,
    debugger_image: str = "gt-memory-env:latest",
    max_hits_per_event: int = 64,
) -> tuple[CommandResult, list[dict], bool]:
    """Run one PoC and return `(gdb result, hits, reachability checked)`."""
    output_dir.mkdir(parents=True, exist_ok=True)
    breakpoints_path = output_dir / "reachability_breakpoints.json"
    hits_path = output_dir / "reachability_hits.json"
    write_breakpoint_spec(checkpoints, breakpoints_path)
    hits_path.unlink(missing_ok=True)

    gdb_script = repo_root / "evaluator" / "reachability" / "gdb_reachability.py"
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
        f"REACHABILITY_MAX_HITS_PER_BREAKPOINT={max_hits_per_event}",
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
        str(prepared.executable),
        str(poc_path.resolve()),
    ]
    proc = _run(command, timeout=timeout)
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
    loaded_hits = load_hits(hits_path) if hits_path.is_file() else []
    checked = (
        proc.returncode == 0
        and hits_path.is_file()
        and not any(hit.get("run_error") for hit in loaded_hits)
    )
    return result, loaded_hits, checked
