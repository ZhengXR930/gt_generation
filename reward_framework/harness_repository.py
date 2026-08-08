"""Versioned isolated OpenHands fork used between training episodes."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .state_store import atomic_json, utc_now


PATCHABLE_PREFIXES = (
    "openhands/controller/",
    "openhands/agenthub/codeact_agent/",
    "openhands/memory/",
)
PATCHABLE_EXACT = {"openhands/core/loop.py", "openhands/core/main.py"}
FORBIDDEN_LITERALS = ("gt_results", "poc_results", "sanitizer_trace", "arvo_", "secbench_", "osv_")
FORBIDDEN_ADDITIONS = (
    "max_iterations", "iteration_budget", "model_name", "llm_config",
    "requests.get", "requests.post", "urllib.request", "curl ", "wget ",
)


def _files(root: Path) -> dict[str, bytes]:
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("OpenHands harness worktree cannot contain symbolic links")
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


class HarnessRepository:
    def __init__(self, root: Path, pristine_openhands: Path):
        self.root = root.resolve()
        self.pristine = pristine_openhands.resolve()
        self.worktree = self.root / "worktree"
        self.versions = self.root / "versions"
        self.active_path = self.root / "active.json"

    def initialize(self) -> int:
        if not self.pristine.joinpath("openhands").is_dir():
            raise FileNotFoundError(f"invalid pristine OpenHands checkout: {self.pristine}")
        self.root.mkdir(parents=True, exist_ok=True)
        self.versions.mkdir(parents=True, exist_ok=True)
        if not self.worktree.is_dir():
            shutil.copytree(
                self.pristine, self.worktree,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv"),
            )
        if not self.active_path.is_file():
            atomic_json(self.active_path, {
                "version": 1, "created_at": utc_now(), "parent": None,
                "changed_files": [], "source_sha256": self.source_sha256(),
            })
            atomic_json(self.versions / "v0001.json", json.loads(self.active_path.read_text()))
        return self.active_version

    @property
    def active_version(self) -> int:
        return int(json.loads(self.active_path.read_text())["version"])

    def source_sha256(self) -> str:
        digest = hashlib.sha256()
        for relative, content in sorted(_files(self.worktree).items()):
            if relative.startswith(".harness_optimizer/"):
                continue
            digest.update(relative.encode() + b"\0" + content)
        return digest.hexdigest()

    def snapshot(self) -> dict[str, bytes]:
        return _files(self.worktree)

    def restore(self, snapshot: dict[str, bytes]) -> None:
        for path in self.worktree.rglob("*"):
            if path.is_symlink():
                path.unlink()
        current = _files(self.worktree)
        for relative in set(current) - set(snapshot):
            (self.worktree / relative).unlink()
        for relative, content in snapshot.items():
            path = self.worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    @staticmethod
    def patchable(relative: str) -> bool:
        return relative in PATCHABLE_EXACT or relative.startswith(PATCHABLE_PREFIXES)

    def validate_changes(self, before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
        changed = sorted(
            relative for relative in set(before) | set(after)
            if before.get(relative) != after.get(relative)
            and not relative.startswith(".harness_optimizer/")
        )
        if not changed:
            raise ValueError("Harness Patcher reported a patch but changed no source")
        forbidden = [relative for relative in changed if not self.patchable(relative)]
        if forbidden:
            raise ValueError(f"unpatchable OpenHands files changed: {forbidden}")
        for relative in changed:
            path = self.worktree / relative
            if not path.is_file():
                raise ValueError("Harness Patcher cannot delete harness files")
            text = path.read_text(encoding="utf-8")
            if any(token.lower() in text.lower() for token in FORBIDDEN_LITERALS):
                raise ValueError(f"sample-specific literal in harness patch: {relative}")
            if path.suffix == ".py":
                ast.parse(text, filename=relative)
            old_text = before.get(relative, b"").decode("utf-8", errors="replace")
            additions = [
                line[2:] for line in difflib.ndiff(
                    old_text.splitlines(), text.splitlines()
                ) if line.startswith("+ ")
            ]
            if any(
                token.lower() in line.lower()
                for line in additions for token in FORBIDDEN_ADDITIONS
            ):
                raise ValueError(
                    f"forbidden harness objective/config addition: {relative}"
                )
        return changed

    def accept(self, *, before: dict[str, bytes], changed: list[str],
               categories: list[str], model: str) -> dict[str, Any]:
        version = self.active_version + 1
        version_dir = self.versions / f"v{version:04d}"
        version_dir.mkdir(parents=True, exist_ok=False)
        diffs = []
        for relative in changed:
            old = before.get(relative, b"").decode("utf-8", errors="replace").splitlines(True)
            new = (self.worktree / relative).read_text(encoding="utf-8").splitlines(True)
            diffs.extend(difflib.unified_diff(old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}"))
            target = version_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.worktree / relative, target)
        (version_dir / "patch.diff").write_text("".join(diffs), encoding="utf-8")
        record = {
            "version": version, "parent": version - 1, "created_at": utc_now(),
            "changed_files": changed, "failure_categories": categories,
            "patcher_model": model, "source_sha256": self.source_sha256(),
        }
        atomic_json(version_dir / "revision.json", record)
        atomic_json(self.versions / f"v{version:04d}.json", record)
        atomic_json(self.active_path, record)
        return record
