"""One-time public issue + source -> stage Reward Spec initialization."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .assertion_reward import RewardSpec, validate_spec_sources
from .backend import RewardAgentBackend
from .models import TaskContext
from .source_view import (
    materialize_source_view,
    resolve_public_source_path,
    write_source_index,
)
from .state_store import atomic_json

SCHEMA = Path(__file__).resolve().with_name("schemas") / "assertion_spec.json"

SPEC_PROMPT = """You are the initialization role of an external Reward Agent
for vulnerability reproduction. Your complete information boundary is the
public issue below and the vulnerable source files in the current directory.

Do not access parent directories, absolute paths, environment variables,
network resources, tests, separately supplied harness/build metadata, commit
history, patches, known testcases, sanitizer traces, ground truth, or model
memory about a known vulnerability. Source drivers present in the current
codebase are ordinary public source and may be inspected. `SOURCE_INDEX.md` is
an automatically generated index from the same public issue and source tree;
read it first and inspect the relevant source files before deciding claims. Do
not construct a PoC and do not provide repair advice.

Return a compact executable Reward Spec with exactly these top-level fields:
`admission`, `source`, `root`, `propagation`, and `sink`.

1. `admission`: one to three alternative source locations where a real public
   API, parser, decoder, dispatcher, or in-tree fuzz driver accepts candidate
   input and dispatches/constructs the issue-relevant internal object. These
   locations are OR alternatives.
2. `source`: source/input-derivation locations where issue-relevant
   attacker-controlled fields enter internal state.
3. `root`: vulnerable-state predicates that must hold for the issue-described
   bug to exist at runtime.
4. `propagation.required`: ordered producer -> consumer transitions that are
   necessary to connect source/root to sink. Keep this sparse.
5. `propagation.optional`: useful but non-essential propagation checkpoints.
6. `sink`: dangerous consumption predicates where the vulnerable state is used.

Use only eq, ne, lt, le, gt, ge, or same_object for `check`. Operands and `via`
items are short side-effect-free source expressions visible at the cited
location, or literal spellings such as `0`, `true`, or `nullptr`. Do not use
function calls or assignments. Prefer one decisive root claim and one decisive
sink claim. Leave uncertain propagation under `optional` or empty. Do not
predict sanitizer output. Every location must use a real source-relative file,
function, and current line number.

The vulnerable source view is the source/ directory. Inspect it, but write
citation paths relative to source/ (do not include the source/ prefix).

PUBLIC ISSUE (verbatim):
{issue}
"""


def require_minimum_reward_spec(spec: RewardSpec) -> None:
    """Reject structurally valid but operationally useless Reward Specs."""
    missing = []
    if not spec.admission:
        missing.append("admission")
    if not spec.source:
        missing.append("source")
    if not spec.root:
        missing.append("root")
    if not spec.sink:
        missing.append("sink")
    if missing:
        raise ValueError(
            "Reward Spec is missing required stage claims: " + ", ".join(missing)
        )


def canonicalize_spec_sources(spec: RewardSpec, source_root: Path) -> RewardSpec:
    """Repair only unambiguous wrapper/path spellings in model citations."""
    value = spec.to_dict()
    for stage in ("admission", "source", "root", "sink"):
        normalized = []
        for item in value[stage]:
            copied = dict(item)
            copied["at"] = {
                **copied["at"],
                "file": resolve_public_source_path(
                    source_root, copied["at"]["file"], copied["at"]["function"]
                ),
            }
            normalized.append(copied)
        value[stage] = normalized
    for group in ("required", "optional"):
        normalized = []
        for item in value["propagation"][group]:
            copied = dict(item)
            copied["from"] = {
                **copied["from"],
                "file": resolve_public_source_path(
                    source_root,
                    copied["from"]["file"],
                    copied["from"]["function"],
                ),
            }
            copied["to"] = {
                **copied["to"],
                "file": resolve_public_source_path(
                    source_root,
                    copied["to"]["file"],
                    copied["to"]["function"],
                ),
            }
            normalized.append(copied)
        value["propagation"][group] = normalized
    return RewardSpec.from_dict(value)


class SpecAgent:
    def __init__(self, backend: RewardAgentBackend,
                 cache_root: Path | None = None):
        self.backend = backend
        self.cache_root = cache_root.resolve() if cache_root else None

    def _cache_path(self, *, issue: str, manifest: str) -> Path | None:
        if self.cache_root is None:
            return None
        material = json.dumps({
            "schema_version": 4,
            "issue": issue,
            "source_manifest_sha256": manifest,
            "spec_prompt_sha256": hashlib.sha256(
                SPEC_PROMPT.encode("utf-8")
            ).hexdigest(),
            "model": self.backend.model,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.cache_root / f"{hashlib.sha256(material).hexdigest()}.json"

    def initialize(self, *, task_id: str, issue_description: str,
                   codebase_root: Path, agent_root: Path) -> TaskContext:
        issue = issue_description.strip()
        if not issue:
            raise ValueError("issue description cannot be empty")
        source_view = agent_root / "source"
        count, manifest = materialize_source_view(codebase_root, source_view)
        if count == 0:
            raise ValueError("source view contains no eligible source files")
        write_source_index(source_view, agent_root, issue)
        cache_path = self._cache_path(issue=issue, manifest=manifest)
        if cache_path is not None and cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if set(cached) != {"reward_spec"}:
                raise ValueError(f"invalid Reward Spec cache entry: {cache_path}")
            spec = RewardSpec.from_dict(cached["reward_spec"])
            require_minimum_reward_spec(spec)
            validate_spec_sources(spec, source_view)
            return TaskContext(
                task_id=task_id,
                issue_description=issue,
                codebase_root=str(codebase_root.resolve()),
                source_manifest_sha256=manifest,
                reward_spec=spec,
                spec_model=self.backend.model,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        base_prompt = SPEC_PROMPT.format(issue=issue)
        error: Exception | None = None
        for attempt in range(3):
            correction = ""
            if error is not None:
                correction = (
                    "\nYour previous structured result failed deterministic source "
                    f"validation: {error}. Reinspect the cited public source and "
                    "return a corrected complete Spec. Do not weaken a claim merely "
                    "to bypass validation.\n"
                )
            raw = self.backend.run_json(
                role="initialize_spec" if attempt == 0 else "repair_spec",
                prompt=base_prompt + correction,
                schema=SCHEMA,
                cwd=agent_root,
            )
            try:
                spec = canonicalize_spec_sources(RewardSpec.from_dict(raw), source_view)
                require_minimum_reward_spec(spec)
                validate_spec_sources(spec, source_view)
                break
            except (ValueError, TypeError) as exc:
                error = exc
        else:
            raise ValueError(f"Reward Spec failed source validation after repair: {error}")
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(cache_path, {"reward_spec": spec.to_dict()})
        return TaskContext(
            task_id=task_id,
            issue_description=issue,
            codebase_root=str(codebase_root.resolve()),
            source_manifest_sha256=manifest,
            reward_spec=spec,
            spec_model=self.backend.model,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
