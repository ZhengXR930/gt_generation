"""Create the source-only view visible to the Reward Agent."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp",
    ".rs", ".go", ".java", ".js", ".ts", ".rb", ".php",
}
HIDDEN_PARTS = {
    ".git", ".svn", "node_modules", "target", "build", "dist", "out",
    "afl", "benchmark", "benchmarks", "example", "examples",
    "script", "scripts", "test", "tests",
    "testapp", "testapps", "testing", "gt_results", "poc_results",
}


def hidden_name(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered in HIDDEN_PARTS
        or lowered.startswith("afl")
        or lowered.startswith("test")
        or "benchmark" in lowered
    )


def eligible_source_files(root: Path) -> list[Path]:
    root = root.resolve()
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [name for name in dirnames if not hidden_name(name)]
        base = Path(directory)
        for name in filenames:
            if hidden_name(Path(name).stem):
                continue
            path = base / name
            if path.suffix.lower() in SOURCE_SUFFIXES and not path.is_symlink():
                files.append(path)
    return sorted(files)


def materialize_source_view(source_root: Path, destination: Path) -> tuple[int, str]:
    source_root = source_root.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    manifest = hashlib.sha256()
    count = 0
    for path in eligible_source_files(source_root):
        relative = path.relative_to(source_root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_bytes()
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        manifest.update(relative.as_posix().encode())
        manifest.update(b"\0")
        manifest.update(digest.encode())
        manifest.update(b"\n")
        count += 1
    return count, manifest.hexdigest()


def refresh_agent_documents(agent_root: Path, documents: dict[str, Path]) -> None:
    """Copy controller-owned JSON inputs into an otherwise isolated view."""
    agent_root.mkdir(parents=True, exist_ok=True)
    for name, source in documents.items():
        if not source.is_file():
            continue
        target = agent_root / name
        shutil.copy2(source, target)
