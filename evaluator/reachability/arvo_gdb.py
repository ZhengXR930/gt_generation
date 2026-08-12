"""Run exact GT reachability breakpoints for one ARVO target under GDB."""

from __future__ import annotations

import fcntl
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

try:  # Support package imports and the historical evaluator-on-PYTHONPATH mode.
    from .engine import CommandResult, load_hits, write_breakpoint_spec
except ImportError:  # pragma: no cover - compatibility for existing CLI entrypoints
    from reachability.engine import CommandResult, load_hits, write_breakpoint_spec

_TARGET_RE = re.compile(rb"/out/([^\s'\"]+)")
_AFL_MARKER = b"This binary is built for AFL-fuzz"


@dataclass(frozen=True)
class PreparedTarget:
    root: Path
    executable: Path
    container_id: str = ""


def target_arguments(prepared: PreparedTarget, poc_path: Path) -> list[str]:
    """Return harness-aware arguments for AFL and libFuzzer ARVO targets."""
    if prepared.container_id:
        probe = _run([
            "docker", "exec", prepared.container_id, "/bin/sh", "-lc",
            "grep -aq 'This binary is built for AFL-fuzz' "
            + str(prepared.executable) + "; printf '%s' $?",
        ])
        is_afl = probe.stdout.strip() == "0"
    else:
        try:
            is_afl = _AFL_MARKER in prepared.executable.read_bytes()
        except OSError:
            is_afl = False
    arguments = [str(prepared.executable)]
    if not is_afl:
        arguments.append("-runs=0")
    arguments.append(str(poc_path.resolve()))
    return arguments


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
def prepare_arvo_target(
    image: str,
    *,
    repo_root: Path | None = None,
    debugger_image: str = "gt-memory-env:latest",
) -> Iterator[PreparedTarget]:
    """Prepare a target without separating it from its runtime rootfs.

    When ``repo_root`` is supplied, GDB is provisioned in a disposable
    container made from the vulnerable image.  This preserves the target's ELF
    interpreter and shared libraries.  Extraction remains as a compatibility
    fallback for older callers.
    """
    if repo_root is not None:
        with _prepare_native_target(
            image, repo_root=repo_root, debugger_image=debugger_image
        ) as prepared:
            yield prepared
        return

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


@contextmanager
def _prepare_native_target(
    image: str, *, repo_root: Path, debugger_image: str
) -> Iterator[PreparedTarget]:
    """Run the debugger inside the exact vulnerable-image runtime."""
    container_id = ""
    debugger_bundle = _ensure_debugger_bundle(debugger_image)
    try:
        inspected = _run(["docker", "image", "inspect", image])
        if inspected.returncode != 0:
            pulled = _run(["docker", "pull", image], timeout=1800)
            if pulled.returncode != 0:
                raise RuntimeError(
                    f"could not pull vulnerable image {image}: "
                    f"{pulled.stderr.strip() or pulled.stdout.strip()}"
                )
        created = _run([
            "docker", "create", "--platform", "linux/amd64",
            "--cap-add", "SYS_PTRACE",
            "--security-opt", "seccomp=unconfined",
            "-v", f"{repo_root}:{repo_root}",
            "-v", f"{debugger_bundle}:/opt/reachability-gdb:ro",
            "--entrypoint", "/bin/sh", image,
            "-c", "while :; do sleep 3600; done",
        ])
        if created.returncode != 0:
            raise RuntimeError(
                f"docker create failed for {image}: {created.stderr.strip()}"
            )
        container_id = created.stdout.strip()
        started = _run(["docker", "start", container_id])
        if started.returncode != 0:
            raise RuntimeError(
                f"docker start failed for {image}: {started.stderr.strip()}"
            )
        target = _run([
            "docker", "exec", container_id, "/bin/sh", "-lc",
            "grep -aoE '/out/[A-Za-z0-9_.-]+' /bin/arvo | head -1",
        ])
        executable = target.stdout.strip()
        if target.returncode != 0 or not executable.startswith("/out/"):
            raise RuntimeError(f"could not identify fuzz target in {image}:/bin/arvo")

        yield PreparedTarget(
            root=Path("/"),
            executable=Path(executable),
            container_id=container_id,
        )
    finally:
        if container_id:
            _run(["docker", "rm", "-f", container_id])


def _ensure_debugger_bundle(debugger_image: str) -> Path:
    """Materialize a reusable GDB runtime without modifying target images."""
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", "/tmp"))
    bundle = cache_root / "gt-reachability" / "gdb-bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    lock_path = bundle.parent / "gdb-bundle.lock"
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        complete = bundle / ".complete"
        python_module = (
            bundle / "usr" / "share" / "gdb" / "python" / "gdb" / "__init__.py"
        )
        if complete.is_file() and python_module.is_file():
            return bundle
        script = r"""
set -eu
deps=$(ldd /usr/bin/gdb | awk '/=> \// {print $3} /^\// {print $1}')
cp --parents -L /usr/bin/gdb /lib64/ld-linux-x86-64.so.2 $deps /bundle
mkdir -p /bundle/usr/share
cp -a /usr/share/gdb /bundle/usr/share/
if [ -d /usr/lib/python3.12 ]; then
  mkdir -p /bundle/usr/lib
  cp -a /usr/lib/python3.12 /bundle/usr/lib/
fi
if [ -e /lib/x86_64-linux-gnu/libthread_db.so.1 ]; then
  cp --parents -L /lib/x86_64-linux-gnu/libthread_db.so.1 /bundle
fi
touch /bundle/.complete
"""
        built = _run([
            "docker", "run", "--rm",
            "-v", f"{bundle}:/bundle",
            "--entrypoint", "/bin/sh", debugger_image, "-lc", script,
        ], timeout=180)
        if built.returncode != 0 or not complete.is_file():
            raise RuntimeError(
                "could not construct reusable GDB bundle: "
                f"{built.stderr.strip() or built.stdout.strip()}"
            )
        return bundle


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
    try:
        hits_path.unlink()
    except FileNotFoundError:
        pass

    gdb_script = repo_root / "evaluator" / "reachability" / "gdb_reachability.py"
    if prepared.container_id:
        bundled_gdb = [
            "/opt/reachability-gdb/lib64/ld-linux-x86-64.so.2",
            "--library-path",
            "/opt/reachability-gdb/lib/x86_64-linux-gnu:"
            "/opt/reachability-gdb/usr/lib/x86_64-linux-gnu",
            "/opt/reachability-gdb/usr/bin/gdb",
        ]
        command = [
            "docker", "exec", "-e", "HOME=/tmp",
            "-e", "PYTHONHOME=/opt/reachability-gdb/usr",
            "-e", "ASAN_OPTIONS=detect_leaks=0",
            "-e", f"REACHABILITY_BREAKPOINTS={breakpoints_path}",
            "-e", f"REACHABILITY_OUTPUT={hits_path}",
            "-e", f"REACHABILITY_MAX_HITS_PER_BREAKPOINT={max_hits_per_event}",
            "-w", str(repo_root), prepared.container_id,
            *bundled_gdb,
            "--data-directory=/opt/reachability-gdb/usr/share/gdb",
            "--batch", "-q", "-x", str(gdb_script), "--args",
            *target_arguments(prepared, poc_path),
        ]
    else:
        command = [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp", "-e", "ASAN_OPTIONS=detect_leaks=0",
            "-e", f"LD_LIBRARY_PATH={prepared.executable.parent}",
            "-e", f"REACHABILITY_BREAKPOINTS={breakpoints_path}",
            "-e", f"REACHABILITY_OUTPUT={hits_path}",
            "-e", f"REACHABILITY_MAX_HITS_PER_BREAKPOINT={max_hits_per_event}",
            "-v", f"{repo_root}:{repo_root}",
            "-v", f"{prepared.root}:{prepared.root}:ro",
            "-w", str(repo_root), debugger_image,
            "gdb", "--batch", "-q", "-x", str(gdb_script), "--args",
            *target_arguments(prepared, poc_path),
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
