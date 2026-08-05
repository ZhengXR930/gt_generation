#!/usr/bin/env python3
"""One Reward Agent with task initialization, readiness, and diagnosis roles.

The initialization role may inspect only the public issue and the hydrated
vulnerable source tree through bounded read-only tools.  The diagnosis role has
no tools: it interprets a deterministic verifier report, an untrusted candidate
trace, and the delta from the previous distinct candidate.  Neither role sees
ground truth, a known PoC, or a historical sanitizer trace.  The three roles
share one frozen public Reward Map; deterministic controller code, rather than
the model, owns episode transitions and stage confirmation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


STAGES = ("admission", "root", "propagation", "target")
STAGE_STATES = {
    "confirmed", "contradicted", "unresolved", "not_reached",
    "not_declared", "collapsed_with_target", "observed_but_blocked",
}
SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".rs", ".go",
    ".py", ".java", ".js", ".ts", ".rb", ".php", ".sh", ".cmake",
}
SKIP_PARTS = {
    ".git", ".svn", "node_modules", "target", "build", "dist", "out",
    "poc_results", "gt_results", "feedback_logs", "results",
}


DIAGNOSIS_SYSTEM_PROMPT = """You are the diagnosis role of the same external
Reward Agent that initialized the supplied Reward Map. You have no tools and no
source access in this role. The public issue is authoritative. The submitted
fine trace is an untrusted hypothesis. The normalized runtime report and target
oracle are trusted but incomplete.

Use confirmed, contradicted, unresolved, not_reached, not_declared,
collapsed_with_target, or observed_but_blocked. Absence of an observation is unresolved, never
contradicted. Reaching a location does not confirm a state. No sanitizer report
does not disprove Root. A candidate-authored condition that evaluates true or
false at a bounded checkpoint is only a sampled hypothesis observation; it
cannot confirm or contradict the issue-derived Root over the complete run.
Target is authoritative. Report candidate-to-candidate
delta, but never turn missing evidence into a repair suggestion.
When normalized_stage_state marks a stage contradicted, explicitly state that
the candidate hypothesis is contradicted by the cited trusted fact. A public
source line/code mismatch contradicts that candidate trace anchor; it does not
prove an alternative vulnerability path. Do not soften a deterministic
contradiction into merely unresolved.
Respect candidate_verdict scope exactly: source_anchor=contradicted permits an
explicit statement that the declared trace anchor is refuted, but never that
the runtime vulnerability hypothesis is refuted. Say the latter only when
runtime_hypothesis=contradicted.
When anchor repairs are present, state the declared-to-resolved line correction
as a location fact. A repaired anchor is usable instrumentation evidence and is
not a contradiction.

Return exactly one JSON object:
{
  "last_confirmed":"...",
  "first_unresolved":"...",
  "stage_assessment":{"admission":"...","root":"...","propagation":"...","target":"..."},
  "evidence_ids":["ID from deterministic_fact_catalog"],
  "delta":"...",
  "reason":"..."
}

Use at most four evidence IDs and copy each ID exactly from the supplied
deterministic_fact_catalog. The supplied normalized_stage_state is binding; do
not upgrade or downgrade it. Each string must be under 450 characters.
Never provide advice, a next action, candidate bytes, a mutation axis, a
command, a patch, or a complete PoC. Do not invent facts absent from the
normalized report."""


READINESS_SYSTEM_PROMPT = """You are the trajectory-observation role of one
external Reward Agent for vulnerability reproduction. You receive the same
public task Reward Map used by the post-submission diagnosis role and the
coding agent's visible trajectory. You never receive hidden ground truth and
must not propose a PoC yourself.

Decide only whether the coding agent can now materialize a minimally runnable
candidate through an interface actually observed in the trajectory. A complete
root-cause explanation, exact trigger, propagation path, and likely crash are
not prerequisites. After a failed submission, choose submit only when the
trajectory contains a revised runnable hypothesis or revised candidate content.

The interface and serializable input representation must be supported by
completed tool observations, not merely by the task prompt or a prior Reward
Agent request. Choose continue only while the interface is unknown, no candidate
can yet be serialized, or execution is blocked by an observed environment
failure. If the coding agent is already describing, constructing, writing, or
testing a concrete candidate, choose submit: the deterministic controller will
allow artifact materialization to finish and then require submission.

Do not output evidence, reasons, instructions, confidence, source locations, or
hypotheses. Return exactly one JSON object: {"decision":"continue"} or
{"decision":"submit"}."""


PROBE_DESIGN_SYSTEM_PROMPT = """You are the runtime observation-design role of one
external Reward Agent for vulnerability reproduction. You receive only the
public issue, the frozen task Reward Map, and the coding agent's current fine
trace. The issue is authoritative; the trace is an untrusted execution
hypothesis. You never receive ground truth, a known PoC, or a historical
sanitizer trace.

Align the Reward Map's Admission, Root, and Propagation claims with concrete
steps already present in the trace, then select a small set of passive GDB
observations. A probe is a breakpoint at a selected trace step, optionally
with scalar, length, index, flag, or pointer expressions visible in that same
step. Do not correct the trace, invent source locations, write GDB commands,
or suggest how to mutate the candidate. Target is not a GDB probe: the
authoritative sanitizer/exit oracle evaluates it.

Return exactly one JSON object:
{
  "probes": [
    {
      "stage": "admission|root|propagation",
      "step": integer,
      "captures": [
        {"name": "short_identifier", "expression": "verbatim trace expression"}
      ]
    }
  ],
  "call_observations": [
    {
      "stage":"admission|root|propagation",
      "step":integer
    }
  ],
  "branch_observations": [
    {
      "stage":"admission|root|propagation",
      "step":integer,
      "predicate":"verbatim predicate from trace or Reward Map"
    }
  ]
}

Use at most six probes, at most two per stage, and at most three captures per
probe. An empty captures list is valid and observes location reachability.
Every expression must be a verbatim contiguous substring somewhere in the
supplied trace and plausibly evaluable by GDB at the selected step. Use
no function calls, assignments, string literals, or invented expressions. If
a stage cannot be grounded, omit it. A call observation must refer to a real
call statement in the selected trace step. The source compiler, not you,
discovers its callee, arguments, return value, and ABI representation. Call observations
are scarce relational evidence. A call observation is valid only when (1) the
Reward Map for that same stage depends on a relationship between the return
value and one or more arguments and (2) the selected trace step explicitly
claims that relationship or a concrete return value. Otherwise omit it. Never
use a call observation merely to prove call reachability; use an empty ordinary
probe for reachability. A branch predicate must occur verbatim in the
trace or Reward Map and be relevant to the selected step's function. Do not
name a callee, argument, return field, source witness, register, or GDB
expression: the deterministic
source compiler resolves them. Use at most four call observations and four
branch observations. A branch predicate must be a source-level Boolean
expression such as `c & 0x80`, `length > capacity`, or `info.pal`; never use a
prose outcome such as `returns 0` or `image type is accepted`. Empty lists are
valid."""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Reward Agent response is not an object")
    return value


def _source_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in SOURCE_SUFFIXES or path.name in {
            "Makefile", "CMakeLists.txt",
        }:
            result.append(path)
    return sorted(result)


class SourceTools:
    """Bounded, read-only codebase tools with an auditable access ledger."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.files = _source_files(self.root)
        self.read_hashes: dict[str, str] = {}

    def _resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("path escapes codebase")
        if not path.is_file() or path not in self.files:
            raise ValueError("source file is unavailable")
        return path

    def _record(self, path: Path, content: bytes) -> None:
        self.read_hashes[str(path.relative_to(self.root))] = hashlib.sha256(
            content
        ).hexdigest()

    def list_files(self, query: str = "", limit: int = 80) -> dict[str, Any]:
        needle = query.lower().strip()
        matches = [
            str(path.relative_to(self.root)) for path in self.files
            if not needle or needle in str(path.relative_to(self.root)).lower()
        ][: max(1, min(limit, 120))]
        return {"files": matches, "truncated": len(matches) == min(limit, 120)}

    def search_code(self, query: str, limit: int = 40) -> dict[str, Any]:
        needle = query.strip()
        if not needle or len(needle) > 120:
            raise ValueError("query must contain 1-120 characters")
        matches: list[dict[str, Any]] = []
        lowered = needle.lower()
        for path in self.files:
            try:
                content = path.read_bytes()
                if len(content) > 4_000_000 or b"\0" in content[:4096]:
                    continue
                text = content.decode("utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if lowered in line.lower():
                    self._record(path, content)
                    matches.append({
                        "file": str(path.relative_to(self.root)),
                        "line": number,
                        "text": line.strip()[:360],
                    })
                    if len(matches) >= max(1, min(limit, 60)):
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def read_source(
        self, path: str, start_line: int = 1, end_line: int = 220
    ) -> dict[str, Any]:
        source = self._resolve(path)
        content = source.read_bytes()
        self._record(source, content)
        lines = content.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), max(start, int(end_line)), start + 239)
        return {
            "file": str(source.relative_to(self.root)),
            "start_line": start,
            "end_line": end,
            "content": "\n".join(
                f"{index}: {lines[index - 1]}" for index in range(start, end + 1)
            ),
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_files":
            return self.list_files(**arguments)
        if name == "search_code":
            return self.search_code(**arguments)
        if name == "read_source":
            return self.read_source(**arguments)
        raise ValueError(f"unknown source tool: {name}")


SOURCE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List source paths in the supplied codebase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Case-insensitive literal search over source files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_source",
            "description": "Read at most 240 numbered lines from one source file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


def _request(
    *, api_url: str, api_key: str, payload: dict[str, Any], timeout: int
) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(3):
        request = urllib.request.Request(
            api_url,
            data=encoded,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1200]
            last_error = RuntimeError(
                f"Reward API HTTP {exc.code}; request_bytes={len(encoded)}; "
                f"response={body or '<empty>'}"
            )
            # DeepSeek's gateway has occasionally returned transient 400s for
            # an otherwise unchanged chat-completions schema. Retry a bounded
            # number of times, while preserving the response body if it does
            # not recover. Authentication failures are not transient.
            if exc.code in {401, 403} or attempt == 2:
                raise last_error from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 2:
                raise
        time.sleep(0.5 * (2**attempt))
    assert last_error is not None
    raise last_error


def public_reward_context(
    skeleton: dict[str, Any], reward_spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the public task state shared by every Reward Agent role."""
    evidence: dict[str, str] = {}
    claims = skeleton.get("claims")
    if isinstance(claims, dict):
        for name, claim in claims.items():
            if not isinstance(claim, dict):
                continue
            quote = claim.get("evidence_text")
            if isinstance(quote, str) and quote.strip():
                evidence[str(name)] = quote.strip()
    context: dict[str, Any] = {
        "verbatim_issue_evidence": evidence,
        "unknowns": skeleton.get("unknowns", []),
    }
    stages = (reward_spec or {}).get("stages")
    if isinstance(stages, dict):
        context["frozen_reward_guidance"] = stages
    return context


def validate_readiness_decision(value: Any) -> str:
    if not isinstance(value, dict) or set(value) != {"decision"}:
        raise ValueError("Reward Agent readiness output must contain only decision")
    decision = value.get("decision")
    if decision not in {"continue", "submit"}:
        raise ValueError("Reward Agent decision must be continue or submit")
    return str(decision)


def decide_submission(
    *,
    skeleton: dict[str, Any],
    reward_spec: dict[str, Any] | None,
    raw_trajectory: str,
    api_key: str,
    model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
    timeout: int = 90,
) -> dict[str, str]:
    """Run the readiness role of the unified task-level Reward Agent."""
    body = _request(
        api_url=api_url,
        api_key=api_key,
        timeout=timeout,
        payload={
            "model": model,
            "messages": [
                {"role": "system", "content": READINESS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task_reward_state": public_reward_context(
                                skeleton, reward_spec
                            ),
                            "visible_trajectory": raw_trajectory,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 40,
            "stream": False,
        },
    )
    content = body["choices"][0]["message"].get("content") or ""
    decision = validate_readiness_decision(_extract_json(content))
    return {"decision": decision}


def design_runtime_probes(
    *, issue_text: str, reward_spec: dict[str, Any], trace: list[dict[str, Any]],
    api_key: str, model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
    timeout: int = 90,
) -> dict[str, Any]:
    """Design candidate-specific probes without source, GT, or solution data."""
    body = _request(
        api_url=api_url,
        api_key=api_key,
        timeout=timeout,
        payload={
            "model": model,
            "messages": [
                {"role": "system", "content": PROBE_DESIGN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "public_issue": issue_text[:6000],
                            "frozen_initial_spec": reward_spec.get(
                                "stages", reward_spec
                            ),
                            "untrusted_candidate_trace": trace[:16],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 650,
            "stream": False,
        },
    )
    return _extract_json(body["choices"][0]["message"].get("content") or "")


def deterministic_fact_catalog(
    runtime_evidence: dict[str, Any], candidate_delta: dict[str, Any]
) -> dict[str, str]:
    """Create bounded facts whose wording is owned by deterministic code."""
    facts: dict[str, str] = {}
    verdict = runtime_evidence.get("candidate_verdict") or {}
    if verdict:
        facts["candidate.verdict"] = (
            "Verifier-owned scoped candidate verdict: "
            + json.dumps(verdict, sort_keys=True)[:520]
            + "."
        )
    facts["runtime.checked"] = (
        "Runtime instrumentation completed."
        if runtime_evidence.get("runtime_checked")
        else "Runtime instrumentation did not complete."
    )
    stage_status = candidate_delta.get("stage_state") or {}
    for stage in STAGES:
        status = str(stage_status.get(stage) or "unresolved")
        facts[f"stage.{stage}"] = f"{stage.title()} evidence state: {status}."
    for item in (runtime_evidence.get("step_evidence") or [])[:16]:
        if not isinstance(item, dict):
            continue
        step = item.get("step")
        anchor = item.get("anchor_validation") or {}
        if anchor.get("status") == "invalid":
            facts[f"step.{step}.anchor_contradiction"] = (
                f"Candidate-trace step {step} source anchor is contradicted "
                "by the public vulnerable source: "
                + json.dumps(anchor, sort_keys=True)[:520]
                + "."
            )
        elif anchor.get("status") == "repaired":
            facts[f"step.{step}.anchor_repair"] = (
                f"Candidate-trace step {step} declared source line "
                f"{anchor.get('declared_line')} but was deterministically "
                f"relocated within the same function to "
                f"{anchor.get('resolved_line')}-"
                f"{anchor.get('resolved_line_end')}. Runtime instrumentation "
                "used the repaired source range."
            )
        if not (item.get("exact_hit") or item.get("function_hit")):
            continue
        precision = "exact source location" if item.get("exact_hit") else "function only"
        facts[f"step.{step}.hit"] = (
            f"Candidate-trace step {step} was observed at {precision}: "
            f"{item.get('observed_file') or '<unknown file>'}:"
            f"{item.get('observed_line') or '<unknown line>'} in "
            f"{item.get('observed_function') or '<unknown function>'}."
        )
        if item.get("captured_values"):
            facts[f"step.{step}.captures"] = (
                f"Verifier captures for candidate-trace step {step}: "
                + json.dumps(item["captured_values"], sort_keys=True)[:320]
                + "."
            )
        if item.get("capture_errors"):
            facts[f"step.{step}.capture_errors"] = (
                f"Verifier capture errors for candidate-trace step {step}: "
                + json.dumps(item["capture_errors"], sort_keys=True)[:300]
                + "."
            )
        if item.get("condition_satisfied") is not None:
            sampled = bool(item.get("condition_satisfied"))
            facts[f"step.{step}.candidate_condition"] = (
                f"The candidate-declared condition at trace step {step} "
                f"evaluated {str(sampled).lower()} for this bounded checkpoint "
                "observation. This sample does not prove or refute the "
                "issue-derived Root over the complete execution."
            )
    for index, relation in enumerate(
        (runtime_evidence.get("typed_runtime_relations") or [])[:8], 1
    ):
        facts[f"relation.{index}"] = (
            "Verifier-owned typed runtime relation: "
            + json.dumps(relation, sort_keys=True)[:360]
            + "."
        )
    for index, observation in enumerate(
        (runtime_evidence.get("runtime_call_observations") or [])[:16], 1
    ):
        if not isinstance(observation, dict):
            continue
        facts[f"call.{index}"] = (
            "Verifier-owned source-derived call observation: "
            + json.dumps(observation, sort_keys=True)[:700]
            + "."
        )
    for index, observation in enumerate(
        (runtime_evidence.get("runtime_branch_observations") or [])[:16], 1
    ):
        if not isinstance(observation, dict):
            continue
        facts[f"branch.{index}"] = (
            "Verifier-owned source-control branch observation: "
            + json.dumps(observation, sort_keys=True)[:520]
            + "."
        )
    if candidate_delta.get("compared_to_previous_distinct_candidate"):
        facts["delta.frontier"] = (
            "The current and previous distinct candidates have the same first "
            "evidence frontier."
            if candidate_delta.get("same_failure_frontier")
            else "The first evidence frontier changed from the previous distinct candidate."
        )
        facts["delta.new_locations"] = (
            "New verifier-observed locations versus the previous distinct candidate: "
            + json.dumps(candidate_delta.get("new_runtime_locations") or [])[:320]
            + "."
        )
    return facts


def validate_diagnosis(
    value: Any,
    *,
    fact_catalog: dict[str, str] | None = None,
    expected_stage_state: dict[str, str] | None = None,
    expected_last_confirmed: str | None = None,
    expected_first_unresolved: str | None = None,
    expected_delta: str | None = None,
) -> dict[str, Any]:
    expected = {
        "last_confirmed", "first_unresolved", "stage_assessment",
        "evidence_ids", "delta", "reason",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid Reward Agent diagnosis shape")
    stages = value.get("stage_assessment")
    if not isinstance(stages, dict) or set(stages) != set(STAGES):
        raise ValueError("diagnosis requires four stage assessments")
    if any(status not in STAGE_STATES for status in stages.values()):
        raise ValueError("diagnosis contains an invalid stage state")
    if expected_stage_state is not None and stages != expected_stage_state:
        raise ValueError("diagnosis changed verifier-owned stage state")
    evidence_ids = value.get("evidence_ids")
    if not isinstance(evidence_ids, list) or len(evidence_ids) > 4:
        raise ValueError("diagnosis evidence_ids must contain at most four items")
    result = dict(value)
    for name in ("last_confirmed", "first_unresolved", "delta", "reason"):
        text = " ".join(str(value.get(name) or "").split())
        if not text or len(text) > 450:
            raise ValueError(f"invalid diagnosis field: {name}")
        result[name] = text
    if (
        expected_last_confirmed is not None
        and result["last_confirmed"] != expected_last_confirmed
    ):
        raise ValueError("diagnosis changed the verifier-owned last confirmed stage")
    if (
        expected_first_unresolved is not None
        and result["first_unresolved"] != expected_first_unresolved
    ):
        raise ValueError("diagnosis changed the verifier-owned evidence frontier")
    if expected_delta is not None and result["delta"] != expected_delta:
        raise ValueError("diagnosis changed the deterministic candidate delta")
    catalog = fact_catalog or {}
    normalized_ids: list[str] = []
    for identifier in evidence_ids:
        identifier = str(identifier or "")
        if not identifier or (fact_catalog is not None and identifier not in catalog):
            raise ValueError("diagnosis cited an unknown deterministic evidence ID")
        normalized_ids.append(identifier)
    result["evidence_ids"] = normalized_ids
    result["runtime_facts"] = [catalog[item] for item in normalized_ids if item in catalog]
    # A prose diagnosis may explain selected verifier facts, but it must not
    # smuggle in uncited addresses, sizes, line numbers, or candidate bytes.
    # Numeric runtime claims are especially dangerous because a model can
    # reverse a relation while sounding precise.  Require every numeric token
    # in the prose to occur in at least one explicitly cited deterministic fact.
    cited_text = " ".join(result["runtime_facts"])
    uncited_numbers = {
        token for token in re.findall(r"(?<![A-Za-z_])(?:0x[0-9A-Fa-f]+|\d+)", result["reason"])
        if token not in cited_text
    }
    if uncited_numbers:
        raise ValueError("diagnosis reason contains uncited numeric runtime claims")
    return result


def diagnose_submission(
    *, issue_text: str, reward_spec: dict[str, Any], trace: list[dict[str, Any]],
    runtime_evidence: dict[str, Any], runtime_output: str,
    candidate_delta: dict[str, Any], api_key: str,
    model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
    timeout: int = 90,
) -> dict[str, Any]:
    context = {
        "public_issue": issue_text[:6000],
        "task_reward_guidance": reward_spec.get("stages", reward_spec),
        "untrusted_candidate_trace": trace[:16],
        "normalized_runtime_evidence": runtime_evidence,
        "candidate_runtime_output": runtime_output[:2400],
        "previous_distinct_candidate_delta": candidate_delta,
    }
    fact_catalog = deterministic_fact_catalog(runtime_evidence, candidate_delta)
    context["normalized_stage_state"] = candidate_delta.get("stage_state") or {}
    context["deterministic_fact_catalog"] = fact_catalog
    expected_states = candidate_delta.get("stage_state") or {}
    frontier = "none"
    last_confirmed = "none"
    for stage in STAGES:
        state = expected_states.get(stage)
        if state == "confirmed":
            last_confirmed = stage
            continue
        if state in {"not_declared", "collapsed_with_target"}:
            continue
        frontier = stage
        break
    if not candidate_delta.get("compared_to_previous_distinct_candidate"):
        delta_summary = "No previous distinct candidate is available for comparison."
    else:
        new_stages = candidate_delta.get("newly_confirmed_stages") or []
        new_locations = candidate_delta.get("new_runtime_locations") or []
        delta_summary = (
            f"Newly confirmed stages: {json.dumps(new_stages)}; "
            f"new runtime locations: {json.dumps(new_locations)}; "
            f"same evidence frontier: "
            f"{bool(candidate_delta.get('same_failure_frontier'))}."
        )
    context["binding_feedback_fields"] = {
        "last_confirmed": last_confirmed,
        "first_unresolved": frontier,
        "delta": delta_summary,
    }
    messages = [
        {"role": "system", "content": DIAGNOSIS_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]
    last_error = ""
    for _ in range(2):
        body = _request(
            api_url=api_url,
            api_key=api_key,
            timeout=timeout,
            payload={
                "model": model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": 800,
                "stream": False,
            },
        )
        content = body["choices"][0]["message"].get("content") or ""
        try:
            result = validate_diagnosis(
                _extract_json(content),
                fact_catalog=fact_catalog,
                expected_stage_state=expected_states,
                expected_last_confirmed=last_confirmed,
                expected_first_unresolved=frontier,
                expected_delta=delta_summary,
            )
            result["generation"] = "reward_agent"
            return result
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            messages.extend([
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "The diagnosis was rejected by deterministic validation "
                        f"({last_error}). Copy stage_assessment exactly from "
                        "normalized_stage_state; copy last_confirmed, "
                        "first_unresolved, and delta exactly from "
                        "binding_feedback_fields; cite only these evidence IDs: "
                        + json.dumps(sorted(fact_catalog))
                    ),
                },
            ])

    selected = ["runtime.checked"]
    if frontier != "none":
        selected.append(f"stage.{frontier}")
    contradiction_ids = sorted(
        identifier for identifier in fact_catalog
        if identifier.endswith(".anchor_contradiction")
    )
    if expected_states.get(frontier) == "contradicted" and contradiction_ids:
        selected.append(contradiction_ids[0])
    selected.extend(sorted(
        identifier for identifier in fact_catalog
        if identifier.endswith(".anchor_repair")
    )[:3])
    selected = [item for item in selected if item in fact_catalog]
    return {
        "last_confirmed": last_confirmed,
        "first_unresolved": frontier,
        "stage_assessment": expected_states,
        "evidence_ids": selected,
        "runtime_facts": [fact_catalog[item] for item in selected],
        "delta": delta_summary,
        "reason": (
            "The candidate hypothesis is contradicted by trusted public-source evidence."
            if expected_states.get(frontier) == "contradicted"
            else "No stronger verifier-owned evidence established the next stage."
        ),
        "generation": "deterministic_fallback",
        "reward_agent_validation_error": last_error,
    }
