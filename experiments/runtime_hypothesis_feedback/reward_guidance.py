"""Reward guidance v2: three-pass generation of an issue-derived semantic map.

Why this exists alongside `reward_agent.py`
-------------------------------------------
The v1 Reward Map asks the model for prose plus function-level anchors and then
designs the actual instrumentation at runtime from the *subject's* trace. The
schema-study specs went the other way and froze `file:function:line` plus C
capture expressions. Both failed, from opposite directions:

* Frozen captures: 27% of them came back `invalid_verifier` at runtime because
  the expression was optimized out or out of scope at the chosen stop, and a
  static re-audit of the 10 study specs found a further 17/93 anchored on the
  very statement that assigns the captured value, so the probe silently reads
  the pre-assignment value. Both facts live in the compiled binary, not in the
  source text the model was given.
* Prose-only anchors: the frozen artifact carries no executable content of its
  own, so the only thing that can be instrumented is what the subject declared.
  A reward that has to borrow the subject's hypothesis cannot tell "the trace is
  wrong" from "the guidance is wrong".

v2 splits the artifact by who can actually answer each question. The model
states semantics only: which function, which conceptual state, what relation is
expected. A deterministic compiler (not in this module) resolves that to
breakpoints and readable expressions against the vulnerable build's DWARF, and
reports `unbindable` rather than guessing. Every trust label is computed by the
harness -- there is no field the model fills in about its own confidence,
because two separate attempts at that (`confidence`, then `support.status` with
verbatim issue quotes) both mislabelled a known-wrong binding.

Information boundary
--------------------
Unchanged from v1: the public issue text and the vulnerable codebase, nothing
else. No GT, no known PoC, no sanitizer trace beyond what the issue quotes, no
patch, no fixed source, no commit history, no network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from reward_agent import (  # noqa: E402
    SOURCE_TOOL_DEFINITIONS,
    SourceTools,
    _extract_json,
    _request,
)

SCHEMA_VERSION = "public-reward-guidance-v2"
STAGES = ("admission", "root", "propagation", "target")
PROPAGATION_MODES = {"distinct", "collapsed_with_target", "not_declared"}

# Observation points a deterministic compiler can resolve without the model
# guessing a line number. `call:<callee>` means the call site of that callee
# inside the anchored function.
SIMPLE_POINTS = {"entry", "return"}
CALL_POINT = re.compile(r"^call:[A-Za-z_][A-Za-z0-9_]*$")

# Typed semantic relations. The model names the relation; the compiler decides
# how to evaluate it against a bound expression.
EXPECT_RELATIONS = {
    "uninitialized",
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "unchecked",
    "aliased_after_free",
    "out_of_bounds",
    "present",
}
RELATIONS_NEEDING_OPERAND = {"eq", "ne", "lt", "le", "gt", "ge"}

# Support labels are computed from cross-sample agreement, never self-reported.
SUPPORT_AGREED = "agreed"
SUPPORT_MAJORITY = "majority"
SUPPORT_AMBIGUOUS = "ambiguous"
SUPPORT_NOT_DECLARED = "not_declared"


DRAFT_PROMPT = """You are the drafting role of an external Reward Agent for
vulnerability reproduction. Your entire information boundary is the public issue
below and the vulnerable codebase reachable through the read-only tools. Do not
use outside knowledge, network resources, known testcases, ground truth,
patches, fixed code, commit history, or sanitizer traces beyond text quoted in
the issue itself.

Produce a semantic Reward Guidance: WHERE to observe and WHAT to expect there.
You do NOT write line numbers and you do NOT write source expressions. A
deterministic compiler resolves both against the compiled vulnerable target,
because whether a value is readable at a given machine instruction is a property
of the build, not of the source text you can see.

Four stages:
- admission: the real input interface accepts the candidate and materializes the
  issue-relevant internal object. Not process start, not the fuzzer entry point.
- root: the vulnerable state claimed by the issue is established. Reaching a
  function is not a state.
- propagation: that state is consumed by a later issue-relevant operation. Use
  mode collapsed_with_target when the root operation is itself the dangerous
  consumption, and not_declared when issue plus source cannot support a distinct
  downstream consumer.
- target: the authoritative issue-relevant runtime violation.

Return exactly one JSON object:
{
  "admission": {
    "claim": "one sentence",
    "where": [{"file": "...", "function": "...", "point": "entry"}],
    "observe": []
  },
  "root": {
    "claim": "one sentence",
    "where": [{"file": "...", "function": "...", "point": "call:some_callee"}],
    "observe": [{"name": "what the value means, in words",
                 "why": "the issue phrase that requires it",
                 "expect": "gt", "operand": 0}]
  },
  "propagation": {"mode": "distinct|collapsed_with_target|not_declared",
                  "claim": "...", "where": [...], "observe": [...]},
  "target": {"claim": "...", "where": [...], "observe": []}
}

Rules:
- "point" is exactly "entry", "return", or "call:<callee-name>". Nothing else.
  Pick the point whose control-flow meaning alone carries the claim where you
  can; that needs no value at all.
- "observe[].name" is a natural-language description of the value or state, for
  example "the element count parsed from the chunk header". Never a C
  expression, never a variable name dressed up as prose.
- "expect" is one of: uninitialized, eq, ne, lt, le, gt, ge, unchecked,
  aliased_after_free, out_of_bounds, present. Comparison relations also need an
  "operand" (a JSON number, or a string describing another observed value).
- At most 3 "where" entries and at most 3 "observe" entries per stage.
- Paths must come from tool results and the function must occur in that file.
- Use an empty "where" and empty "observe" when a stage is not supportable. An
  empty target is fine: the runtime oracle is authoritative anyway.
- All four stages must describe one internally consistent path. Re-check any
  arithmetic in the root claim against the actual downstream dispatch
  predicates. A logical extent is not an allocated extent.
- Do not include serialized input, magic bytes, field assignments, a candidate
  value combination, a testcase-shaped example, a PoC, or repair advice.
  Describe input and state classes only."""


AUDIT_PROMPT = """You are the independent source-audit role of an external Reward
Agent. Inspect only the public issue, the draft guidance, and the vulnerable
codebase through the read-only tools. Do not use outside knowledge, network
resources, known testcases, ground truth, patches, fixed code, commit history,
or sanitizer traces.

Audit the draft as an executable causal argument rather than editing its prose.
For each stage check that the anchored function is genuinely on the path the
issue describes, and that the observed state named at that point is the one the
issue requires. Evaluate stated operations with normal language semantics --
integer truncation, overflow, signedness, short-circuiting -- and verify the
resulting value satisfies the exact source dispatch predicate that reaches the
named consumer. Verify memory claims against allocation extent, not logical
extent. A pre-operation value must not be silently reused after an operation
transforms it.

If a link lacks direct issue plus source support, set propagation mode to
not_declared. If the precise target operation is unsupported, return an empty
target "where". Never replace an unsupported path with a plausible alternative
and never widen a stage to make it satisfiable.

Return exactly one JSON object in the same four-stage schema. Remove concrete
candidate strings, bytes, field assignments, value combinations,
testcase-shaped examples, and repair advice."""


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

def _validate_where(entry: Any, source: SourceTools) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise ValueError("where entry must be an object")
    relative = str(entry.get("file") or "").strip()
    function = str(entry.get("function") or "").strip()
    point = str(entry.get("point") or "").strip()
    if not relative or not function:
        raise ValueError("where entry needs file and function")
    if point not in SIMPLE_POINTS and not CALL_POINT.match(point):
        raise ValueError(
            f"point must be entry, return, or call:<callee>; got {point!r}"
        )
    path = source._resolve(relative)
    content = path.read_bytes()
    source._record(path, content)
    text = content.decode("utf-8", errors="replace")
    if function not in text:
        raise ValueError(f"function {function!r} is absent from {relative}")
    if point.startswith("call:"):
        callee = point.split(":", 1)[1]
        if callee not in text:
            raise ValueError(f"callee {callee!r} is absent from {relative}")
    return {"file": relative, "function": function, "point": point}


def _validate_observe(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("observe entry must be an object")
    name = str(entry.get("name") or "").strip()
    expect = str(entry.get("expect") or "").strip()
    if not name:
        raise ValueError("observe entry needs a name")
    if expect not in EXPECT_RELATIONS:
        raise ValueError(f"expect must be one of {sorted(EXPECT_RELATIONS)}; got {expect!r}")
    # The whole point of v2 is that the model does not author source syntax.
    # Reject anything that looks like an expression rather than a description.
    if re.search(r"(->|\[|\]|\(|\)|;|\+\+|--|::)", name):
        raise ValueError(f"observe name must be prose, not a source expression: {name!r}")
    result: dict[str, Any] = {
        "name": name,
        "why": str(entry.get("why") or "").strip(),
        "expect": expect,
    }
    if expect in RELATIONS_NEEDING_OPERAND:
        if "operand" not in entry:
            raise ValueError(f"relation {expect!r} needs an operand")
        operand = entry["operand"]
        if not isinstance(operand, (int, float, str)):
            raise ValueError("operand must be a number or a description string")
        result["operand"] = operand
    return result


def validate_guidance(value: Any, source: SourceTools) -> dict[str, Any]:
    """Structural gate. Raises ValueError; never repairs silently."""
    if not isinstance(value, dict) or set(value) != set(STAGES):
        raise ValueError(f"guidance must contain exactly the stages {list(STAGES)}")
    result: dict[str, Any] = {}
    for stage in STAGES:
        item = value.get(stage)
        if not isinstance(item, dict):
            raise ValueError(f"{stage} must be an object")
        expected = {"claim", "where", "observe"}
        if stage == "propagation":
            expected.add("mode")
        if set(item) != expected:
            raise ValueError(f"{stage} keys must be exactly {sorted(expected)}")

        claim = str(item.get("claim") or "").strip()
        where_raw = item.get("where")
        observe_raw = item.get("observe")
        if not isinstance(where_raw, list) or not isinstance(observe_raw, list):
            raise ValueError(f"{stage} where/observe must be lists")
        if len(where_raw) > 3 or len(observe_raw) > 3:
            raise ValueError(f"{stage} allows at most 3 where and 3 observe entries")
        if not claim:
            raise ValueError(f"{stage} needs a claim")

        normalized: dict[str, Any] = {
            "claim": claim,
            "where": [_validate_where(w, source) for w in where_raw],
            "observe": [_validate_observe(o) for o in observe_raw],
        }
        if not normalized["where"] and normalized["observe"]:
            raise ValueError(f"{stage} declares observations with no location")

        if stage == "propagation":
            mode = item.get("mode")
            if mode not in PROPAGATION_MODES:
                raise ValueError(f"propagation mode must be one of {sorted(PROPAGATION_MODES)}")
            normalized["mode"] = mode
            if mode == "not_declared" and (normalized["where"] or normalized["observe"]):
                raise ValueError("not_declared propagation must not carry evidence")
        result[stage] = normalized
    return result


# --------------------------------------------------------------------------- #
# Pass 1 and Pass 2                                                            #
# --------------------------------------------------------------------------- #

def _run_agent(
    *, system_prompt: str, user_content: str, source: SourceTools,
    api_key: str, model: str, api_url: str, timeout: int, max_rounds: int,
) -> dict[str, Any]:
    """One bounded tool-using session that must end in schema-valid JSON."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    last_failure = "no final JSON object was returned"
    # Reserve the tail for synthesis with tools off, otherwise a capable model
    # can browse source for every available turn and never emit the artifact.
    synthesis_start = max(1, max_rounds - 3)
    for round_index in range(max_rounds):
        synthesizing = round_index >= synthesis_start
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": 3000,
            "stream": False,
        }
        if synthesizing:
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["tools"] = SOURCE_TOOL_DEFINITIONS
            payload["tool_choice"] = "auto"

        body = _request(api_url=api_url, api_key=api_key, timeout=timeout, payload=payload)
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        if calls and not synthesizing:
            messages.append(message)
            for call in calls:
                function = (call.get("function") or {})
                name = str(function.get("name") or "")
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    output = source.call(name, args)
                except (ValueError, OSError) as exc:
                    output = {"error": f"{type(exc).__name__}: {exc}"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(output, ensure_ascii=False),
                })
            continue

        text = str(message.get("content") or "")
        try:
            candidate = _extract_json(text)
            return validate_guidance(candidate, source)
        except ValueError as exc:
            last_failure = str(exc)
            messages.append(message if message else {"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": (
                    "The previous object was rejected: " + last_failure
                    + " Return one corrected JSON object and nothing else."
                ),
            })
    raise ValueError(f"reward guidance was not produced: {last_failure}")


def draft_guidance(
    *, issue_text: str, source: SourceTools, api_key: str, model: str,
    api_url: str, timeout: int = 180, max_rounds: int = 10,
) -> dict[str, Any]:
    """Pass 1: issue + source -> semantic guidance."""
    return _run_agent(
        system_prompt=DRAFT_PROMPT,
        user_content="PUBLIC ISSUE (verbatim):\n" + issue_text,
        source=source, api_key=api_key, model=model, api_url=api_url,
        timeout=timeout, max_rounds=max_rounds,
    )


def audit_guidance(
    *, issue_text: str, draft: dict[str, Any], source: SourceTools,
    api_key: str, model: str, api_url: str, timeout: int = 180,
    max_rounds: int = 6,
) -> dict[str, Any]:
    """Pass 2: an independent session re-checks the draft against source."""
    return _run_agent(
        system_prompt=AUDIT_PROMPT,
        user_content=(
            "PUBLIC ISSUE (verbatim):\n" + issue_text
            + "\n\nDRAFT GUIDANCE (untrusted):\n"
            + json.dumps(draft, ensure_ascii=False, indent=2)
        ),
        source=source, api_key=api_key, model=model, api_url=api_url,
        timeout=timeout, max_rounds=max_rounds,
    )


# --------------------------------------------------------------------------- #
# Pass 3: agreement-based support                                              #
# --------------------------------------------------------------------------- #

def _anchor_key(stage_value: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """Identity of a stage for agreement purposes: its ordered anchor set."""
    return tuple(sorted(
        (w["file"], w["function"], w["point"]) for w in stage_value.get("where") or []
    ))


def agreement_support(samples: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    """Pass 3: keep only what independent runs agree on.

    Asking the model how confident it is has been tried twice and failed twice:
    `confidence` was dropped after wrong hypotheses came back medium/high, and a
    later `support.status` field with verbatim issue quotes still labelled the
    known-wrong arvo_16457 Rice-decoder binding `source_disambiguated`. Repeated
    sampling turns that question into a mechanical one -- if independent runs
    anchor different functions, the issue does not determine the binding, and no
    self-report is needed to establish it.

    Returns the merged guidance plus a per-stage support label. A stage without
    a majority is emptied, so it produces diagnostics and never reward.
    """
    if not samples:
        raise ValueError("agreement needs at least one sample")
    total = len(samples)
    threshold = total // 2 + 1

    merged: dict[str, Any] = {}
    support: dict[str, str] = {}
    for stage in STAGES:
        values = [s[stage] for s in samples]
        if stage == "propagation":
            modes = Counter(v.get("mode") for v in values)
            mode, mode_count = modes.most_common(1)[0]
            if mode_count < threshold or mode == "not_declared":
                merged[stage] = {
                    "mode": "not_declared", "claim":
                        "The public issue and inspected source do not agree on a "
                        "distinct, independently observable propagation stage.",
                    "where": [], "observe": [],
                }
                support[stage] = (
                    SUPPORT_NOT_DECLARED if mode == "not_declared" else SUPPORT_AMBIGUOUS
                )
                continue
            values = [v for v in values if v.get("mode") == mode]

        keys = Counter(_anchor_key(v) for v in values)
        key, count = keys.most_common(1)[0]
        if not key or count < threshold:
            empty: dict[str, Any] = {
                "claim": "Independent drafts did not agree on where this stage occurs.",
                "where": [], "observe": [],
            }
            if stage == "propagation":
                empty["mode"] = "not_declared"
            merged[stage] = empty
            support[stage] = SUPPORT_AMBIGUOUS
            continue

        # Representative: the first agreeing sample. Its observations are kept
        # only where the same relation is also the agreed one, so a lone run
        # cannot smuggle in an expectation the others did not make.
        agreeing = [v for v in values if _anchor_key(v) == key]
        chosen = json.loads(json.dumps(agreeing[0]))
        relation_votes = Counter(
            (o["name"].lower(), o["expect"])
            for v in agreeing for o in v.get("observe") or []
        )
        kept = [
            o for o in chosen.get("observe") or []
            if relation_votes[(o["name"].lower(), o["expect"])] >= (len(agreeing) // 2 + 1)
        ]
        chosen["observe"] = kept
        merged[stage] = chosen
        support[stage] = (
            f"{SUPPORT_AGREED}_{count}_of_{total}" if count == total
            else f"{SUPPORT_MAJORITY}_{count}_of_{total}"
        )
    return merged, support


# --------------------------------------------------------------------------- #
# Accessors                                                                    #
# --------------------------------------------------------------------------- #
#
# Consumers go through these instead of digging into the artifact. v1 spread raw
# `spec["reward_map"][stage]["anchors"]` lookups across the proxy, the trace
# mapper and the experiment runner, so changing the schema meant a repo-wide
# sweep. Anything a consumer needs from a frozen guidance should get a function
# here rather than a dict path at the call site.

LOCATION_STAGES = ("admission", "root", "propagation")


def stages_of(spec: dict[str, Any] | None) -> dict[str, Any]:
    """The stage map of a frozen guidance, or {} when absent."""
    value = (spec or {}).get("stages")
    return value if isinstance(value, dict) else {}


def stage_anchors(
    spec: dict[str, Any] | None, stages: tuple[str, ...] = LOCATION_STAGES
) -> set[tuple[str, str]]:
    """(file, function) pairs the guidance points at, for probe placement."""
    found: set[tuple[str, str]] = set()
    known = stages_of(spec)
    for stage in stages:
        for where in (known.get(stage) or {}).get("where") or []:
            if not isinstance(where, dict):
                continue
            file_name = str(where.get("file") or "")
            function = str(where.get("function") or "")
            if file_name and function:
                found.add((file_name, function))
    return found


def stage_points(
    spec: dict[str, Any] | None, stage: str
) -> list[dict[str, str]]:
    """Full observation points for one stage, including the resolvable `point`."""
    return [
        w for w in (stages_of(spec).get(stage) or {}).get("where") or []
        if isinstance(w, dict)
    ]


def propagation_mode(spec: dict[str, Any] | None) -> str:
    """distinct | collapsed_with_target | not_declared."""
    mode = (stages_of(spec).get("propagation") or {}).get("mode")
    return str(mode) if mode in PROPAGATION_MODES else "distinct"


def stage_support(spec: dict[str, Any] | None, stage: str) -> str:
    """Computed agreement label. Never a model self-report."""
    support = (spec or {}).get("support")
    if not isinstance(support, dict):
        return SUPPORT_AMBIGUOUS
    return str(support.get(stage) or SUPPORT_AMBIGUOUS)


def rewardable(spec: dict[str, Any] | None, stage: str) -> bool:
    """Whether a stage may advance reward, as opposed to diagnostics only.

    A stage with no agreed anchor set is explicitly not rewardable: independent
    drafts disagreeing about where it happens means the issue did not determine
    the binding, and rewarding it would be rewarding a coin flip.
    """
    label = stage_support(spec, stage)
    if label in {SUPPORT_AMBIGUOUS, SUPPORT_NOT_DECLARED}:
        return False
    return bool((stages_of(spec).get(stage) or {}).get("where"))


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #

def build_guidance(
    *, sample_id: str, issue_path: Path, codebase: Path, output_path: Path,
    api_key: str, model: str, api_url: str, samples: int = 3,
    timeout: int = 180, force: bool = False,
) -> dict[str, Any]:
    """Run Pass 1+2 `samples` times independently, then Pass 3 over the results."""
    issue_text = issue_path.read_text(encoding="utf-8", errors="replace").strip()
    digest = hashlib.sha256(issue_text.encode("utf-8")).hexdigest()

    if output_path.is_file() and not force:
        cached = json.loads(output_path.read_text(encoding="utf-8"))
        if (
            cached.get("schema_version") == SCHEMA_VERSION
            and cached.get("issue_sha256") == digest
        ):
            return cached

    source = SourceTools(codebase)
    drafts: list[dict[str, Any]] = []
    failures: list[str] = []
    for index in range(samples):
        try:
            draft = draft_guidance(
                issue_text=issue_text, source=source, api_key=api_key,
                model=model, api_url=api_url, timeout=timeout,
            )
            reviewed = audit_guidance(
                issue_text=issue_text, draft=draft, source=source,
                api_key=api_key, model=model, api_url=api_url, timeout=timeout,
            )
            drafts.append(reviewed)
        except (ValueError, RuntimeError) as exc:
            # A failed sample is evidence too: it cannot vote, and the reduced
            # denominator is recorded rather than hidden.
            failures.append(f"sample {index}: {exc}")

    if not drafts:
        raise ValueError("no reward guidance survived drafting: " + "; ".join(failures))

    merged, support = agreement_support(drafts)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "sample_id": sample_id,
        "issue_sha256": digest,
        "stages": merged,
        "support": support,
        "agreement": {
            "requested_samples": samples,
            "usable_samples": len(drafts),
            "failures": failures,
        },
        "source_audit": {
            "root_basename": codebase.resolve().name,
            "files_read": source.read_hashes,
            "read_only_tools": ["list_files", "search_code", "read_source"],
        },
        "provenance": {
            "inputs": ["public_issue", "vulnerable_codebase"],
            "uses_hidden_gt": False,
            "uses_known_testcase": False,
            "model": model,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--issue", type=Path, required=True)
    parser.add_argument("--codebase", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--model", default="gpt-5.4-2026-03-05")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    import os

    api_key = os.environ.get(args.api_key_env or "", "")
    if not api_key:
        parser.error(f"{args.api_key_env} is empty")

    artifact = build_guidance(
        sample_id=args.sample_id, issue_path=args.issue, codebase=args.codebase,
        output_path=args.out, api_key=api_key, model=args.model,
        api_url=args.api_url, samples=args.samples, timeout=args.timeout,
        force=args.force,
    )
    print(json.dumps({
        "sample_id": artifact["sample_id"],
        "support": artifact["support"],
        "agreement": artifact["agreement"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
