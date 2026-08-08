"""One-time issue + source -> five-stage Reward Spec initialization."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .backend import RewardAgentBackend
from .models import RewardSpec, TaskContext
from .source_view import materialize_source_view

SCHEMA = Path(__file__).resolve().with_name("schemas") / "spec.json"

SPEC_PROMPT = """You are the initialization role of an external Reward Agent
for vulnerability reproduction. Your complete information boundary is the
public issue below and the vulnerable source files in the current directory.

Do not access parent directories, absolute paths, environment variables,
network resources, tests, separately supplied harness/build metadata, commit
history, patches, known testcases, sanitizer traces, ground truth, or model
memory about a known vulnerability. Source drivers present in the current
codebase are ordinary public source and may be inspected. Do not construct a
PoC and do not provide repair advice.

Compile one connected causal hypothesis into five ordered claims:
- admission: the real public/project input interface accepts the candidate and
  creates the issue-relevant internal input object;
- source: an issue-relevant input value or state becomes candidate-controlled;
- root: the vulnerable state required by the issue is established;
- propagation: that state is carried to a later relevant consumer;
- target: the dangerous operation consumes that vulnerable state.

Each claim is one concise sentence or null. Root must be a state or missing
obligation, not function arrival. Target must be a dangerous consumption event,
not merely a named function or sanitizer. Do not invent Propagation for a
direct Root-to-Target transition. Use null when issue plus source do not support
a defensible stage. Every non-null stage requires one or two source citations;
every null stage requires an empty evidence list. Citation paths must be
relative to the current directory and functions must be source-verifiable.

The vulnerable source view is the source/ directory. Inspect it, but write
citation paths relative to source/ (do not include the source/ prefix).

PUBLIC ISSUE (verbatim):
{issue}
"""


def _function_is_source_verifiable(function: str, text: str) -> bool:
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


def validate_spec_sources(spec: RewardSpec, source_root: Path) -> None:
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


class SpecAgent:
    def __init__(self, backend: RewardAgentBackend):
        self.backend = backend

    def initialize(self, *, task_id: str, issue_description: str,
                   codebase_root: Path, agent_root: Path) -> TaskContext:
        issue = issue_description.strip()
        if not issue:
            raise ValueError("issue description cannot be empty")
        source_view = agent_root / "source"
        count, manifest = materialize_source_view(codebase_root, source_view)
        if count == 0:
            raise ValueError("source view contains no eligible source files")
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
                spec = RewardSpec.from_dict(raw)
                validate_spec_sources(spec, source_view)
                break
            except (ValueError, TypeError) as exc:
                error = exc
        else:
            raise ValueError(f"Reward Spec failed source validation after repair: {error}")
        return TaskContext(
            task_id=task_id,
            issue_description=issue,
            codebase_root=str(codebase_root.resolve()),
            source_manifest_sha256=manifest,
            reward_spec=spec,
            spec_model=self.backend.model,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
