"""Issue/Spec/trajectory aligned passive runtime observation planning."""

from __future__ import annotations

import re
from pathlib import Path

from .backend import RewardAgentBackend
from .models import ProbePlan


SCHEMA = Path(__file__).resolve().with_name("schemas") / "probe_plan.json"

PROBE_PROMPT = """You are the runtime-observation design role of one persistent
Reward Agent. Read task_context.json, the complete observation_state.json,
current_trace.json, and prior_evidence.json when present. Inspect source/ as
needed. The public issue and frozen Reward Spec are authoritative task claims;
the candidate trace is an untrusted hypothesis.

Align the issue stages with the trace, then select passive observations at two
types of locations: issue anchors that must be checked even if omitted by the
trace, and trace checkpoints that test what the candidate claims. Validate each
file/function/statement against source. If a trace line is stale, resolve the
intended source statement instead of rejecting the whole hypothesis; use the
resolved source in the plan. Never access parent paths, network, GT, patches,
known PoCs, historical crashes, tests, or separately supplied harness metadata.
Source drivers present under source/ are part of the public codebase.

Captures must be short source expressions visible at the selected statement or
function and useful for input length, requested/returned bytes, pointer
identity, allocation/capacity/index, branch state, ownership, or producer to
consumer identity. Do not use function calls, assignments, commands, concrete
candidate bytes, repair suggestions, or invented variables. A location-only
probe may use an empty capture list. Prefer a small connected plan over broad
coverage. Target may be probed for dangerous consumption, but the independent
runtime trigger oracle remains authoritative.

When a stage claim is a runtime state predicate (especially Root), put the
side-effect-free C/C++ expression that directly decides it in `condition`.
Use null when execution of the exact selected statement is itself the claimed
event. Never turn a weak proxy into a condition.
"""


def validate_probe_sources(plan: ProbePlan, source_root: Path) -> None:
    source_root = source_root.resolve()
    for probe in plan.probes:
        path = (source_root / probe.file).resolve()
        if source_root not in path.parents or not path.is_file():
            raise ValueError(f"probe path escapes source view: {probe.file}")
        text = path.read_text(encoding="utf-8", errors="replace")
        function = probe.function.rsplit("::", 1)[-1]
        if probe.function not in text and function not in text:
            raise ValueError(f"probe function absent from source: {probe.function}")
        if probe.statement and probe.statement not in text:
            raise ValueError(f"probe statement absent from source: {probe.statement}")
        for capture in probe.captures:
            if not capture.strip() or any(token in capture for token in (";", "=", "(")):
                raise ValueError(f"unsafe probe capture: {capture!r}")
        if probe.condition:
            condition = probe.condition.strip()
            assignment_like = bool(re.search(r"(?<![!<>=])=(?!=)", condition))
            if not condition or ";" in condition or assignment_like:
                raise ValueError(f"unsafe probe condition: {probe.condition!r}")


class ProbePlanner:
    def __init__(self, backend: RewardAgentBackend):
        self.backend = backend

    def design(self, *, agent_root: Path) -> ProbePlan:
        error: Exception | None = None
        for attempt in range(3):
            correction = ""
            if error is not None:
                correction = (
                    "\nThe previous plan failed deterministic public-source or "
                    f"expression validation: {error}. Resolve the intended current "
                    "source statement and return a corrected complete plan. Do not "
                    "invent a replacement observation.\n"
                )
            raw = self.backend.run_json(
                role="design_probes" if attempt == 0 else "repair_probes",
                prompt=PROBE_PROMPT + correction,
                schema=SCHEMA,
                cwd=agent_root,
            )
            try:
                plan = ProbePlan.from_dict(raw)
                validate_probe_sources(plan, agent_root / "source")
                return plan
            except (ValueError, TypeError) as exc:
                error = exc
        raise ValueError(f"probe plan failed source validation after repair: {error}")
