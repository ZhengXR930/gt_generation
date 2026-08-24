"""Python interpreter selection helpers for harness entrypoints."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _candidate_pythons(repo_root: Path) -> list[Path]:
    candidates = [
        repo_root / "external" / "OpenHands" / ".venv-openhands" / "bin" / "python",
        *sorted(
            Path.home().glob(".cache/pypoetry/virtualenvs/openhands-ai-*/bin/python"),
            reverse=True,
        ),
        *sorted(
            Path("/data00/home/zhengxinran").glob(
                ".cache/pypoetry/virtualenvs/openhands-ai-*/bin/python"
            ),
            reverse=True,
        ),
    ]
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        expanded = candidate.expanduser()
        resolved = expanded.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(expanded)
    return unique


def _version_ok(candidate: Path, min_version: tuple[int, int]) -> bool:
    try:
        completed = subprocess.run(
            [
                str(candidate),
                "-c",
                (
                    "import sys; "
                    f"raise SystemExit(0 if sys.version_info[:2] >= {min_version!r} else 1)"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def ensure_repo_python(repo_root: Path, *, min_version: tuple[int, int] = (3, 11)) -> None:
    """Re-exec the current script under a repo/OpenHands Python when needed."""
    if sys.version_info[:2] >= min_version:
        return
    if not sys.argv or sys.argv[0] in {"-c", "-", ""}:
        return
    current = Path(sys.executable).resolve()
    for candidate in _candidate_pythons(repo_root):
        if candidate == current:
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK) and _version_ok(candidate, min_version):
            os.execv(str(candidate), [str(candidate), *sys.argv])
    raise RuntimeError(
        "Python "
        f"{min_version[0]}.{min_version[1]}+ required; no compatible "
        "OpenHands Python environment found. Run scripts/setup_openhands.sh "
        "or set PATH to a compatible Python runtime."
    )
