"""Create the source-only view visible to the Reward Agent."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path


SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp",
    ".rs", ".go", ".java", ".js", ".ts", ".rb", ".php",
}
ISSUE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
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


def resolve_public_source_path(root: Path, cited: str,
                               function: str | None = None) -> str:
    """Canonicalize an agent citation without guessing between source files.

    Public repositories in these tasks are often wrapped in one or more
    project directories.  Agents consequently cite ``src/foo.c`` while the
    source-only view contains ``project/src/foo.c``.  Accept a citation only
    when exact, suffix, or basename-plus-function matching identifies one
    unique public source file.
    """
    root = root.resolve()
    cleaned = cited.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith("source/"):
        cleaned = cleaned[len("source/"):]
    if not cleaned or cleaned.startswith("/") or ".." in Path(cleaned).parts:
        raise ValueError(f"unsafe public source citation: {cited}")

    exact = (root / cleaned).resolve()
    if root in exact.parents and exact.is_file():
        return exact.relative_to(root).as_posix()

    files = eligible_source_files(root)
    suffix = "/" + cleaned
    candidates = [
        path for path in files
        if ("/" + path.relative_to(root).as_posix()).endswith(suffix)
    ]
    if not candidates:
        candidates = [path for path in files if path.name == Path(cleaned).name]

    if function and not candidates:
        qualified = function.strip()
        unqualified = qualified.rsplit("::", 1)[-1]
        candidates = []
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            if qualified in text or unqualified in text:
                candidates.append(path)
        same_stem = [path for path in candidates if path.stem == Path(cleaned).stem]
        if same_stem:
            candidates = same_stem

    if function and len(candidates) > 1:
        qualified = function.strip()
        unqualified = qualified.rsplit("::", 1)[-1]
        matching = []
        for path in candidates:
            text = path.read_text(encoding="utf-8", errors="replace")
            if qualified in text or unqualified in text:
                matching.append(path)
        candidates = matching

    if len(candidates) != 1:
        relative = [path.relative_to(root).as_posix() for path in candidates[:5]]
        raise ValueError(
            f"public source citation is not uniquely resolvable: {cited}; "
            f"candidates={relative}"
        )
    return candidates[0].relative_to(root).as_posix()


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


def write_source_index(source_root: Path, destination: Path,
                       issue_description: str) -> Path:
    """Write an issue-keyed public source index for the isolated agent view.

    The index is derived only from the same public issue text and source files
    already visible to the Reward Agent.  It narrows inspection without adding
    GT, testcases, sanitizer traces, or build metadata.
    """
    source_root = source_root.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    tokens = {
        token.lower()
        for token in ISSUE_TOKEN.findall(issue_description)
        if len(token) >= 4
    }
    issue_lower = issue_description.lower().strip()
    scored: list[tuple[int, str, list[str]]] = []
    all_files: list[str] = []
    for path in eligible_source_files(source_root):
        relative = path.relative_to(source_root).as_posix()
        all_files.append(relative)
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        score = 0
        hits: list[str] = []
        if issue_lower and issue_lower in lowered:
            score += 50
            hits.append("full issue phrase")
        basename = path.name.lower()
        for token in sorted(tokens):
            token_score = lowered.count(token)
            if token_score:
                score += min(token_score, 20)
                if len(hits) < 8:
                    hits.append(token)
            if token in basename:
                score += 5
        if score:
            scored.append((score, relative, hits))
    scored.sort(key=lambda item: (-item[0], item[1]))

    lines = [
        "# Source Index",
        "",
        "This file is generated from the public issue text and public source tree only.",
        "Use the relevant files first, then inspect other files from the full list if needed.",
        "",
        "## Issue Tokens",
        "",
        ", ".join(sorted(tokens)) or "(none)",
        "",
        "## Relevant Files",
        "",
    ]
    for score, relative, hits in scored[:40]:
        reason = ", ".join(hits[:8])
        lines.append(f"- `{relative}` score={score} hits={reason}")
    if not scored:
        lines.append("- No direct token match; inspect likely parser/decoder/API files from the full list.")
    lines.extend(["", "## All Source Files", ""])
    for relative in all_files:
        lines.append(f"- `{relative}`")
    target = destination / "SOURCE_INDEX.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def refresh_agent_documents(agent_root: Path, documents: dict[str, Path]) -> None:
    """Copy controller-owned JSON inputs into an otherwise isolated view."""
    agent_root.mkdir(parents=True, exist_ok=True)
    for name, source in documents.items():
        if not source.is_file():
            continue
        target = agent_root / name
        shutil.copy2(source, target)
