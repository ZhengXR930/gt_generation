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


SCHEMA_VERSION = "public-reward-map-v1"
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


SPEC_SYSTEM_PROMPT = """You are the initialization role of an external Reward
Agent for vulnerability reproduction. The public issue is authoritative but
may be incomplete. Inspect only the supplied vulnerable codebase with the
read-only tools. Do not use outside knowledge, network resources, known
testcases, ground truth, patches, commit history, or sanitizer traces.

Build a small semantic Reward Map with four stages:
- Admission: the real input interface accepts the candidate and constructs the
  issue-relevant internal input/state.
- Root: the vulnerable state claimed by the issue is established.
- Propagation: that state is consumed by a later issue-relevant operation. Use
  mode collapsed_with_target when Root is itself the dangerous consumption,
  and not_declared when public issue+source cannot support a distinct claim.
- Target: the authoritative issue-relevant runtime violation occurs.

Return exactly one JSON object:
{
  "admission": {"claim":"...", "anchors":[{"file":"...","function":"..."}], "observation":"..."},
  "root": {"claim":"...", "anchors":[{"file":"...","function":"..."}], "observation":"..."},
  "propagation": {"mode":"distinct|collapsed_with_target|not_declared", "claim":"...", "anchors":[{"file":"...","function":"..."}], "observation":"..."},
  "target": {"claim":"...", "anchors":[{"file":"...","function":"..."}], "observation":"authoritative runtime oracle"}
}

Use at most four anchors per stage. Paths must come from tool results. Functions
must occur in the anchored file. Observations are semantic runtime predicates,
not GDB commands. Use an empty anchors list and an empty observation when a
stage is not supportable.

All four stages must describe one internally consistent path. In particular,
re-evaluate any Root arithmetic or example state against the actual downstream
dispatch predicates: do not name a resampler, branch, consumer, allocation
extent, or sink unless the inspected source establishes that the Root state
selects it. A logical format or pixel extent is not necessarily an allocated
memory extent. When the issue and inspected source establish Root but do not
establish a unique downstream consumer, use propagation mode not_declared
instead of completing the path by inference. Target may use an empty anchor
list and the generic authoritative runtime oracle when its precise source
operation is not established.

Do not include any concrete serialized input string, field assignment, magic
bytes, candidate value combination, testcase-shaped example, concrete PoC,
repair advice, or input mutation. Describe input and state classes only."""


SPEC_REVIEW_PROMPT = """You are the independent source-audit role of an external
Reward Agent for vulnerability reproduction. Inspect only the supplied public
issue, draft Reward Map, and vulnerable codebase through the read-only tools.
Do not use outside knowledge, network resources, known testcases, ground truth,
patches, commit history, or sanitizer traces.

Audit the draft as an executable causal argument, rather than editing its prose.
For every Root-to-Propagation claim, evaluate the stated operation using normal
language semantics (including integer truncation, overflow, signedness, and
short-circuiting), then verify the resulting value satisfies the exact source
dispatch predicate and reaches the named consumer. A pre-operation value must
not be silently reused after an operation transforms it. Verify memory claims
against allocation extent, not logical data extent. If any link lacks direct
public issue/source support, set Propagation to not_declared; if the precise
Target operation is unsupported, use an empty Target anchors list and a generic
authoritative runtime oracle. Never replace an unsupported path with a plausible
alternative.

Return exactly one JSON object in the original four-stage schema. Remove
concrete candidate strings, bytes, field assignments, value combinations,
testcase-shaped examples, and repair advice. Claims must describe input/state
classes, not construct a candidate."""


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
    reward_map = (reward_spec or {}).get("reward_map")
    if isinstance(reward_map, dict):
        context["frozen_reward_map"] = reward_map
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
                                "reward_map", reward_spec
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


def _validate_anchor(anchor: Any, source: SourceTools) -> dict[str, str]:
    if not isinstance(anchor, dict) or set(anchor) != {"file", "function"}:
        raise ValueError("anchor requires exactly file and function")
    relative = str(anchor.get("file") or "").strip()
    function = str(anchor.get("function") or "").strip()
    if not relative or not function:
        raise ValueError("anchor file/function must be non-empty")
    path = source._resolve(relative)
    content = path.read_bytes()
    source._record(path, content)
    if function not in content.decode("utf-8", errors="replace"):
        raise ValueError(f"anchor function {function!r} is absent from {relative}")
    return {"file": relative, "function": function}


def validate_reward_map(value: Any, source: SourceTools) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(STAGES):
        raise ValueError("Reward Map must contain exactly four stages")
    result: dict[str, Any] = {}
    for stage in STAGES:
        item = value.get(stage)
        expected = {"claim", "anchors", "observation"}
        if stage == "propagation":
            expected.add("mode")
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError(f"invalid {stage} shape")
        claim = str(item.get("claim") or "").strip()
        observation = str(item.get("observation") or "").strip()
        anchors = item.get("anchors")
        if not claim or not isinstance(anchors, list) or len(anchors) > 4:
            raise ValueError(f"invalid {stage} claim/anchors")
        normalized = {
            "claim": claim,
            "anchors": [_validate_anchor(anchor, source) for anchor in anchors],
            "observation": observation,
        }
        if stage == "propagation":
            mode = item.get("mode")
            if mode not in {"distinct", "collapsed_with_target", "not_declared"}:
                raise ValueError("invalid propagation mode")
            normalized["mode"] = mode
            if mode == "not_declared" and (anchors or observation):
                raise ValueError("not_declared propagation must not invent evidence")
        result[stage] = normalized
    return result


def enforce_public_evidence_boundary(
    reward_map: dict[str, Any], issue_text: str,
) -> tuple[dict[str, Any], list[str]]:
    """Prevent source exploration from silently extending the public claim.

    Source code may verify an issue claim, but choosing one of several plausible
    downstream consumers is itself vulnerability-solving work.  A distinct
    propagation stage is therefore retained only when the public issue names
    every anchored consumer function.  This is a conservative provenance gate,
    not a semantic score: uncertain stages remain explicitly unobserved.
    """
    result = json.loads(json.dumps(reward_map))
    downgrades: list[str] = []
    propagation = result["propagation"]
    if propagation.get("mode") == "distinct":
        anchors = propagation.get("anchors") or []
        named_consumers = bool(anchors) and all(
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(str(anchor.get('function') or ''))}"
                rf"(?![A-Za-z0-9_])",
                issue_text,
            )
            for anchor in anchors
            if str(anchor.get("function") or "")
        )
        if not named_consumers or any(
            not str(anchor.get("function") or "") for anchor in anchors
        ):
            result["propagation"] = {
                "mode": "not_declared",
                "claim": (
                    "The public issue and inspected source do not establish a "
                    "distinct, independently observable propagation stage."
                ),
                "anchors": [],
                "observation": "",
            }
            downgrades.append("propagation_not_publicly_anchored")
    target = result["target"]
    target_anchors = target.get("anchors") or []
    target_publicly_anchored = bool(target_anchors) and all(
        str(anchor.get("function") or "")
        and re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(str(anchor.get('function')))}"
            rf"(?![A-Za-z0-9_])",
            issue_text,
        )
        for anchor in target_anchors
    )
    if not target_publicly_anchored:
        if target_anchors:
            downgrades.append("target_not_publicly_anchored")
        target["anchors"] = []
        target["claim"] = "The authoritative issue-relevant runtime violation occurs."
        target["observation"] = "authoritative runtime oracle"
    return result, downgrades


def generate_reward_map(
    *, sample_id: str, issue_text: str, codebase: Path, api_key: str,
    model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
    timeout: int = 120, max_rounds: int = 10,
) -> dict[str, Any]:
    source = SourceTools(codebase)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SPEC_SYSTEM_PROMPT},
        {"role": "user", "content": "PUBLIC ISSUE (verbatim):\n" + issue_text},
    ]
    last_failure = "no final JSON object was returned"
    # Reserve the final three turns for synthesis/correction with tools
    # disabled. Without this phase boundary an otherwise capable agent can
    # spend every bounded turn browsing source and never emit the artifact.
    synthesis_start = max(1, max_rounds - 3)
    for round_index in range(max_rounds):
        synthesizing = round_index >= synthesis_start
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 2600,
            "stream": False,
        }
        if synthesizing:
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["tools"] = SOURCE_TOOL_DEFINITIONS
            payload["tool_choice"] = "auto"
        body = _request(
            api_url=api_url,
            api_key=api_key,
            timeout=timeout,
            payload=payload,
        )
        message = body["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = str(message.get("content") or "").strip()
            if not content:
                last_failure = "the model returned empty content"
                messages.append({"role": "assistant", "content": ""})
                messages.append({
                    "role": "user",
                    "content": (
                        "Finish the initialization now. Return only the exact "
                        "four-stage JSON object required by the system message."
                    ),
                })
                continue
            try:
                reward_map = validate_reward_map(_extract_json(content), source)
            except (ValueError, json.JSONDecodeError) as exc:
                last_failure = f"{type(exc).__name__}: {exc}"
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "The proposed Reward Map was rejected by deterministic "
                        f"validation ({type(exc).__name__}: {exc}). Return a "
                        "corrected JSON object only; use only source anchors "
                        "already established through the read-only tools."
                    ),
                })
                continue
            reward_map = _review_reward_map(
                issue_text=issue_text,
                draft=reward_map,
                source=source,
                api_key=api_key,
                model=model,
                api_url=api_url,
                timeout=timeout,
            )
            reward_map, policy_downgrades = enforce_public_evidence_boundary(
                reward_map, issue_text,
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "sample_id": sample_id,
                "issue_sha256": hashlib.sha256(issue_text.encode()).hexdigest(),
                "reward_map": reward_map,
                "source_audit": {
                    "root_basename": codebase.resolve().name,
                    "files_read": source.read_hashes,
                    "read_only_tools": ["list_files", "search_code", "read_source"],
                    "policy_downgrades": policy_downgrades,
                },
                "provenance": {
                    "inputs": ["public_issue", "vulnerable_codebase"],
                    "uses_hidden_gt": False,
                    "uses_known_testcase": False,
                },
            }
        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        })
        for call in tool_calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                output = source.call(name, arguments)
            except Exception as exc:
                output = {"error": f"{type(exc).__name__}: {exc}"}
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "name": name,
                "content": json.dumps(output, ensure_ascii=False),
            })
    raise RuntimeError(
        "Reward Agent did not finish initialization; last failure: "
        + last_failure
    )


def _review_reward_map(
    *, issue_text: str, draft: dict[str, Any], source: SourceTools,
    api_key: str, model: str, api_url: str, timeout: int,
    max_rounds: int = 6,
) -> dict[str, Any]:
    """Independently re-check a draft against public source before freezing it."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SPEC_REVIEW_PROMPT},
        {
            "role": "user",
            "content": (
                "PUBLIC ISSUE (verbatim):\n" + issue_text
                + "\n\nDRAFT REWARD MAP (untrusted):\n"
                + json.dumps(draft, ensure_ascii=False)
            ),
        },
    ]
    last_failure = "no reviewed JSON object was returned"
    synthesis_start = max(1, max_rounds - 2)
    for round_index in range(max_rounds):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 2600,
            "stream": False,
        }
        if round_index >= synthesis_start:
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["tools"] = SOURCE_TOOL_DEFINITIONS
            payload["tool_choice"] = "auto"
        body = _request(
            api_url=api_url,
            api_key=api_key,
            timeout=timeout,
            payload=payload,
        )
        message = body["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = str(message.get("content") or "").strip()
            if content:
                try:
                    return validate_reward_map(_extract_json(content), source)
                except (ValueError, json.JSONDecodeError) as exc:
                    last_failure = f"{type(exc).__name__}: {exc}"
                    messages.append({"role": "assistant", "content": content})
            else:
                last_failure = "the audit model returned empty content"
                messages.append({"role": "assistant", "content": ""})
            messages.append({
                "role": "user",
                "content": (
                    "Return the corrected four-stage JSON object now. Preserve "
                    "only causal links established by inspected public source; "
                    "downgrade unsupported links instead of guessing."
                ),
            })
            continue
        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        })
        for call in tool_calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                output = source.call(name, arguments)
            except Exception as exc:
                output = {"error": f"{type(exc).__name__}: {exc}"}
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "name": name,
                "content": json.dumps(output, ensure_ascii=False),
            })
    raise RuntimeError(
        "Reward Map independent source audit did not finish; last failure: "
        + last_failure
    )


def build_reward_map(
    *, sample_id: str, issue_path: Path, codebase: Path, output_path: Path,
    api_key: str, model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
    force: bool = False,
) -> dict[str, Any]:
    issue_text = issue_path.read_text(encoding="utf-8").strip()
    digest = hashlib.sha256(issue_text.encode()).hexdigest()
    if output_path.is_file() and not force:
        cached = json.loads(output_path.read_text(encoding="utf-8"))
        recorded_files = (cached.get("source_audit") or {}).get("files_read") or {}
        source_unchanged = bool(recorded_files)
        for relative, expected_hash in recorded_files.items():
            path = (codebase / str(relative)).resolve()
            try:
                within_root = codebase.resolve() in path.parents
                actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                within_root = False
                actual_hash = ""
            if not within_root or actual_hash != expected_hash:
                source_unchanged = False
                break
        if cached.get("schema_version") == SCHEMA_VERSION and cached.get(
            "issue_sha256"
        ) == digest and source_unchanged:
            bounded, downgrades = enforce_public_evidence_boundary(
                cached["reward_map"], issue_text,
            )
            audit = cached.setdefault("source_audit", {})
            if (
                bounded != cached["reward_map"]
                or audit.get("policy_downgrades") != downgrades
            ):
                cached["reward_map"] = bounded
                audit["policy_downgrades"] = downgrades
                output_path.write_text(
                    json.dumps(cached, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            return cached
    artifact = generate_reward_map(
        sample_id=sample_id,
        issue_text=issue_text,
        codebase=codebase,
        api_key=api_key,
        model=model,
        api_url=api_url,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return artifact


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
        "task_reward_map": reward_spec.get("reward_map", reward_spec),
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
