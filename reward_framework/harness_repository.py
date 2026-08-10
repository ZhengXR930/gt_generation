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
    "openhands/llm/",
)
PATCHABLE_EXACT = {"openhands/core/loop.py", "openhands/core/main.py"}
FORBIDDEN_LITERALS = ("gt_results", "poc_results", "sanitizer_trace", "arvo_", "secbench_", "osv_")
FORBIDDEN_ADDITIONS = (
    "max_iterations", "iteration_budget", "model_name", "llm_config",
    "requests.get", "requests.post", "urllib.request", "curl ", "wget ",
    "use the finish tool",
)

# The evaluation/reward lifecycle wraps these OpenHands methods.  The Patcher
# may change their implementation, but an accepted fork must retain at least
# one call shape understood by that cross-platform lifecycle boundary.
OVERLAY_METHOD_CONTRACTS = {
    "openhands/controller/agent_controller.py": {
        "AgentController": {
            "_handle_action": (2,),
            "_step": (1,),
            "set_agent_state_to": (2,),
            "_is_stuck": (1,),
        },
    },
    "openhands/agenthub/codeact_agent/codeact_agent.py": {
        "CodeActAgent": {
            "step": (2,),
            # Pristine OpenHands takes (self, events); an evolved harness may
            # additionally take state to preserve a pending obligation.
            "_get_messages": (2, 3),
        },
    },
}


class _EraseStringConstants(ast.NodeTransformer):
    """Compare executable Python structure without counting prompt prose."""

    def visit_Constant(self, node: ast.Constant):  # noqa: N802
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def _python_behavior_changed(old_text: str, new_text: str) -> bool:
    try:
        old_tree = _EraseStringConstants().visit(ast.parse(old_text))
        new_tree = _EraseStringConstants().visit(ast.parse(new_text))
    except SyntaxError:
        return False
    return ast.dump(old_tree, include_attributes=False) != ast.dump(
        new_tree, include_attributes=False
    )


def _files(root: Path) -> dict[str, bytes]:
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("OpenHands harness worktree cannot contain symbolic links")
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
        and path.relative_to(root).parts[:1] != ("logs",)
        and path.suffix not in {".pyc", ".log"}
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
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", "*.pyc", ".venv",
                    ".pytest_cache", "logs", "*.log",
                ),
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

    @staticmethod
    def tree_sha256(root: Path) -> str:
        digest = hashlib.sha256()
        for relative, content in sorted(_files(root).items()):
            if relative.startswith(".harness_optimizer/"):
                continue
            digest.update(relative.encode() + b"\0" + content)
        return digest.hexdigest()

    def source_sha256(self) -> str:
        return self.tree_sha256(self.worktree)

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

    def validate_failure_alignment(
        self, *, before: dict[str, bytes], changed: list[str],
        categories: list[str],
    ) -> None:
        """Reject patches whose bytes do not address their declared mechanism.

        This is intentionally a coarse, GT-free contract. It does not decide
        whether a repair will work; it prevents prompt-example edits from
        claiming unrelated controller/parser failures without touching either
        executable behavior or the relevant first-class protocol language.
        Empirical effectiveness is evaluated by later episodes.
        """
        additions: list[str] = []
        executable_paths: set[str] = set()
        for relative in changed:
            old_text = before.get(relative, b"").decode(
                "utf-8", errors="replace"
            )
            new_text = (self.worktree / relative).read_text(encoding="utf-8")
            additions.extend(
                line[2:] for line in difflib.ndiff(
                    old_text.splitlines(), new_text.splitlines()
                ) if line.startswith("+ ")
            )
            if relative.endswith(".py") and _python_behavior_changed(
                old_text, new_text
            ):
                executable_paths.add(relative)
        added = "\n".join(additions).lower()

        if "tool_protocol_recovery_failure" in categories:
            parser_logic = any(
                path.startswith((
                    "openhands/llm/", "openhands/controller/",
                    "openhands/agenthub/codeact_agent/function_calling",
                ))
                for path in executable_paths
            )
            literal_schema_repair = (
                "execute_bash" in added and "command" in added
                and ("function" in added or "tool" in added)
            )
            if not (parser_logic or literal_schema_repair):
                raise ValueError(
                    "tool_protocol_recovery_failure claim does not change "
                    "tool parsing/validation behavior or an execute_bash schema cue"
                )

        materialization_categories = {
            "candidate_materialization_failure", "missing_submission",
            "late_submission",
        }
        if materialization_categories.intersection(categories):
            controller_logic = any(
                path.startswith((
                    "openhands/controller/", "openhands/agenthub/codeact_agent/",
                )) or path in {"openhands/core/loop.py", "openhands/core/main.py"}
                for path in executable_paths
            )
            submission_policy = (
                "submit_candidate" in added
                and any(word in added for word in (
                    "candidate", "materializ", "submission",
                ))
            )
            if not (controller_logic or submission_policy):
                raise ValueError(
                    "candidate-materialization claim does not change controller/"
                    "agent behavior or the first-class submission policy"
                )

        if "submission_context_loss" in categories:
            memory_change = any(path.startswith("openhands/memory/") for path in changed)
            if not (memory_change and "submission" in added):
                raise ValueError(
                    "submission_context_loss claim lacks a submission-aware memory change"
                )

        if "invalid_submission_protocol" in categories:
            submission_routing_change = any(
                path.startswith((
                    "openhands/controller/",
                    "openhands/agenthub/codeact_agent/",
                ))
                for path in executable_paths
            )
            registered_tool_recovery = (
                "dsml" in added
                and any(marker in added for marker in (
                    "submit_candidate", "agent.tools", "self.tools",
                    "registered", "tool_names",
                ))
            )
            if not (submission_routing_change and registered_tool_recovery):
                raise ValueError(
                    "invalid_submission_protocol claim does not change the "
                    "submit_candidate parsing or routing boundary"
                )

        if "premature_finish" in categories:
            termination_logic = any(
                path.startswith("openhands/controller/")
                or path in {"openhands/core/loop.py", "openhands/core/main.py"}
                for path in executable_paths
            )
            if not termination_logic:
                raise ValueError(
                    "premature_finish claim does not change executable controller/"
                    "loop termination behavior"
                )

    @staticmethod
    def _method_accepts_arity(node: ast.FunctionDef | ast.AsyncFunctionDef,
                              arity: int) -> bool:
        positional = [*node.args.posonlyargs, *node.args.args]
        minimum = len(positional) - len(node.args.defaults)
        maximum = None if node.args.vararg is not None else len(positional)
        return minimum <= arity and (maximum is None or arity <= maximum)

    def validate_runtime_contracts(self) -> None:
        """Reject harness forks that break the controller integration ABI."""
        paths = [self.worktree / relative for relative in OVERLAY_METHOD_CONTRACTS]
        # Unit-test/minimal repositories need not emulate the full OpenHands
        # tree. A real fork always contains both integration endpoints.
        if not all(path.is_file() for path in paths):
            return
        for relative, classes in OVERLAY_METHOD_CONTRACTS.items():
            tree = ast.parse(
                (self.worktree / relative).read_text(encoding="utf-8"),
                filename=relative,
            )
            class_nodes = {
                node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
            }
            for class_name, methods in classes.items():
                class_node = class_nodes.get(class_name)
                if class_node is None:
                    raise ValueError(
                        f"harness runtime contract missing class {class_name} in {relative}"
                    )
                method_nodes = {
                    node.name: node for node in class_node.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                for method_name, accepted_arities in methods.items():
                    method = method_nodes.get(method_name)
                    if method is None:
                        raise ValueError(
                            "harness runtime contract missing method "
                            f"{class_name}.{method_name}"
                        )
                    if not any(
                        self._method_accepts_arity(method, arity)
                        for arity in accepted_arities
                    ):
                        raise ValueError(
                            "harness runtime contract incompatible method "
                            f"{class_name}.{method_name}; expected one of positional "
                            f"arities {accepted_arities}"
                        )

    def accept(self, *, before: dict[str, bytes], changed: list[str],
               categories: list[str], model: str) -> dict[str, Any]:
        existing_versions = [
            int(path.stem[1:]) for path in self.versions.glob("v[0-9][0-9][0-9][0-9].json")
            if path.stem[1:].isdigit()
        ]
        version = max([self.active_version, *existing_versions]) + 1
        parent = self.active_version
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
            "version": version, "parent": parent, "created_at": utc_now(),
            "changed_files": changed, "failure_categories": categories,
            "patcher_model": model, "source_sha256": self.source_sha256(),
        }
        atomic_json(version_dir / "revision.json", record)
        atomic_json(self.versions / f"v{version:04d}.json", record)
        atomic_json(self.active_path, record)
        return record
