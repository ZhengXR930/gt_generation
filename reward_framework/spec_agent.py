"""One-time public issue + source -> assertion Reward Spec initialization."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .backend import RewardAgentBackend
from .assertion_reward import AssertionRewardSpec, validate_spec_sources
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

Compile a compact executable Reward Spec with:

1. `admission`: one to three alternative source locations where a real public
   API, parser, decoder, dispatcher, or in-tree fuzz driver accepts candidate
   input and dispatches/constructs the issue-relevant internal object. These
   locations are OR alternatives.
2. `claims`: only source-defensible semantic assertions:
   - required: a safety obligation that correct execution must satisfy. A
     vulnerable candidate makes this check false;
   - observed: an unsafe state that vulnerable execution makes true;
   - transition: an ordered relation from a producer location to a later
     consumer location. The check compares the left expression captured at
     `from` with the right expression captured at `at`.

Use only eq, ne, lt, le, gt, ge, or same_object. Operands are strings containing
short side-effect-free source expressions visible at the cited location, or
literal spellings such as `0`, `true`, or `nullptr`. Do not use function calls
or assignments. Prefer one decisive Required claim and only
add Observed/Transition claims that the public issue and source actually
support. Transition is optional. Do not encode Source/Root/Propagation/Target
labels and do not predict sanitizer output. Every location must use a real
source-relative file, function, and current line number.
The structured `from` field is required by the output contract: set it to null
for Required and Observed claims, and to the producer location for Transition.

The vulnerable source view is the source/ directory. Inspect it, but write
citation paths relative to source/ (do not include the source/ prefix).

PUBLIC ISSUE (verbatim):
{issue}
"""


def _function_is_source_verifiable(function: str, text: str) -> bool:
    if function == "<file>":
        return True
    if function in text:
        return True
    unqualified = function.rsplit("::", 1)[-1]
    if unqualified in text:
        return True
    generated = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)_hash", function)
    return bool(
        generated
        and "funcname##_hash" in text
        and re.search(
            rf"\bSTART\s*\(\s*{re.escape(generated.group(1))}\s*,", text
        )
    )


def _canonicalize_anchor_function(function: str, text: str) -> str:
    if _function_is_source_verifiable(function, text):
        return function
    cited = function.rsplit("::", 1)[-1]
    identifiers = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))
    suffixes = sorted(
        (name for name in identifiers if cited.endswith(name) and len(name) >= 4),
        key=len, reverse=True,
    )
    if suffixes and (len(suffixes) == 1 or len(suffixes[0]) > len(suffixes[1])):
        return suffixes[0]
    # Reward Spec is a soft task hypothesis.  Preserve source-verifiable file
    # evidence when the model's function spelling cannot be repaired; exact
    # executable locations remain mandatory in the later Probe Plan.
    return "<file>"


def validate_legacy_spec_sources(spec, source_root: Path) -> None:
    source_root = source_root.resolve()
    for stage, anchors in spec.evidence.items():
        for anchor in anchors:
            path = (source_root / anchor.file).resolve()
            if source_root not in path.parents or not path.is_file():
                raise ValueError(f"{stage} evidence escapes source view: {anchor.file}")
            text = path.read_text(encoding="utf-8", errors="replace")
            if not _function_is_source_verifiable(anchor.function, text):
                raise ValueError(
                    f"{stage} function {anchor.function!r} absent from {anchor.file}"
                )


def canonicalize_spec_sources(spec, source_root: Path):
    evidence = {}
    for stage, anchors in spec.evidence.items():
        normalized = []
        for anchor in anchors:
            file = resolve_public_source_path(
                source_root, anchor.file, anchor.function
            )
            text = (source_root / file).read_text(
                encoding="utf-8", errors="replace"
            )
            normalized.append(type(anchor)(
                file=file,
                function=_canonicalize_anchor_function(anchor.function, text),
                fact=anchor.fact,
            ))
        evidence[stage] = tuple(normalized)
    return type(spec)(dict(spec.claims), evidence)


def canonicalize_assertion_sources(
    spec: AssertionRewardSpec, source_root: Path,
) -> AssertionRewardSpec:
    """Repair only unambiguous wrapper/path spellings in model citations."""
    value = spec.to_dict()
    value["admission"] = [
        {
            **item,
            "at": {
                **item["at"],
                "file": resolve_public_source_path(
                    source_root, item["at"]["file"], item["at"]["function"]
                ),
            },
        }
        for item in value["admission"]
    ]
    normalized_claims = []
    for item in value["claims"]:
        copied = dict(item)
        copied["at"] = {
            **copied["at"],
            "file": resolve_public_source_path(
                source_root, copied["at"]["file"], copied["at"]["function"]
            ),
        }
        if copied.get("from"):
            copied["from"] = {
                **copied["from"],
                "file": resolve_public_source_path(
                    source_root,
                    copied["from"]["file"],
                    copied["from"]["function"],
                ),
            }
        normalized_claims.append(copied)
    value["claims"] = normalized_claims
    return AssertionRewardSpec.from_dict(value)


class SpecAgent:
    def __init__(self, backend: RewardAgentBackend,
                 cache_root: Path | None = None):
        self.backend = backend
        self.cache_root = cache_root.resolve() if cache_root else None

    def _cache_path(self, *, issue: str, manifest: str) -> Path | None:
        if self.cache_root is None:
            return None
        material = json.dumps({
            "schema_version": 3,
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
            spec = AssertionRewardSpec.from_dict(cached["reward_spec"])
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
                # Every Reward-Agent role shares one durable Codex session, so
                # its working root is stable for the entire episode.
                cwd=agent_root,
            )
            try:
                spec = canonicalize_assertion_sources(
                    AssertionRewardSpec.from_dict(raw), source_view
                )
                if not spec.constructable:
                    raise ValueError(
                        "the assertion Spec is empty despite a non-empty public issue; "
                        "inspect the public source and express defensible assertions"
                    )
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
