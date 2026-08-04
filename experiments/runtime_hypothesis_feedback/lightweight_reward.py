#!/usr/bin/env python3
"""Summarize dense deterministic runtime evidence without solving the task."""

from __future__ import annotations

import json
import urllib.request
from typing import Any


SYSTEM_PROMPT = """You are a lightweight runtime error reporter for
vulnerability reproduction. You do not have source-code access and must not act
as a second PoC-generation agent. The public issue description is the trusted
task contract, though it may be incomplete. The submitting agent's fine trace
is an untrusted hypothesis. Deterministic runtime observations and the real
target result are trusted evidence.

Reward stages are ordered: Admission, Root, Propagation, Target. Report the
first stage whose issue-derived claim is not established by runtime evidence.
Reaching an agent-declared location does not prove its claimed vulnerable
state. Return exactly one JSON object with three short strings:
{
  "last_confirmed": "the strongest stage or substage established by runtime evidence",
  "first_failed": "the first ordered stage or substage not established",
  "reason": "which untrusted trace claim failed or remains unproven, and the runtime facts supporting that judgment"
}

Rules:
- Never invent a branch, value, format rule, source fact, or causal explanation
  that is absent from the supplied evidence.
- Never provide advice, a next action, a mutation axis, candidate bytes, source
  code, a patch, a shell command, a consumer suggestion, or a complete PoC.
- Do not tell the coding agent what to change or try. Only identify the error
  boundary and explain it using current runtime evidence.
- Treat every semantic statement in the fine trace as a claim to test, never as
  evidence. A trace claim may help identify an observation point but cannot
  confirm Admission, Root, Propagation, or Target.
- If evidence is insufficient to explain why execution stopped, state only
  that the claimed condition was not established; do not speculate.
- When captured runtime values directly bear on the failed claim, cite their
  names and concrete values. State a relation between values only when that
  relation is supplied verbatim in typed_runtime_relations. Do
  not replace available boundary/length/state evidence with only a generic
  "no crash" statement.
- Never independently compare raw captured values, even when their numeric
  representations look comparable. scalar_or_pointer is ambiguous and must
  never participate in a comparison. Respect capture_kinds and never compare
  or conflate values of different kinds.
- Root-location reachability is not proof that the vulnerable state exists.
  Target success is authoritative and must not be inferred.
- Step evidence is indexed by submitted trace step. Two trace steps mapped to
  the same source line are independent checkpoint observations; they do not
  mean that the program executed that line twice.
- The number of step-evidence records is never a loop, branch, call, or event
  count. Do not infer execution frequency, an early exit, or a path length from
  repeated locations or from missing records.
- observed_sequence orders only the bounded checkpoint callbacks that were
  recorded. It is not a complete control-flow trace and cannot establish the
  number of intervening executions.
- Keep each field under 450 characters.
"""


def _trim(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def public_trace(trace: list[dict[str, Any]], limit: int = 16) -> list[dict[str, Any]]:
    """Keep agent-authored claims while bounding one online reward request."""
    result: list[dict[str, Any]] = []
    for index, step in enumerate(trace[:limit], 1):
        result.append(
            {
                "step": index,
                "file": _trim(step.get("file"), 180),
                "function": _trim(step.get("function"), 140),
                "role": _trim(step.get("role"), 32),
                "phase": _trim(step.get("phase"), 32),
                "claim": _trim(step.get("note") or step.get("code"), 320),
                "condition": step.get("condition"),
            }
        )
    return result


def verifier_evidence(feedback: dict[str, Any]) -> dict[str, Any]:
    """Expose bounded runtime facts, including observed values and failures."""
    steps = []
    for item in feedback.get("steps") or []:
        if not isinstance(item, dict):
            continue
        steps.append(
            {
                "step": item.get("step"),
                "role": _trim(item.get("role"), 32),
                "exact_hit": bool(item.get("exact_hit")),
                "function_hit": bool(item.get("function_hit")),
                "observed_file": _trim(item.get("observed_file"), 180),
                "observed_function": _trim(item.get("observed_function"), 140),
                "observed_line": item.get("observed_line"),
                "observed_sequence": item.get("observed_sequence"),
                "captured_values": item.get("fields") or {},
                "capture_errors": item.get("capture_errors") or {},
                "capture_sources": item.get("capture_sources") or {},
                "capture_kinds": item.get("observer_capture_kinds") or {},
                "capture_rejections": item.get("capture_rejections") or {},
                "condition_satisfied": item.get("condition_satisfied"),
                "condition_error": item.get("condition_error"),
                "anchor_validation": item.get("anchor_validation"),
                "exact_anchor_error": item.get("exact_anchor_error"),
            }
        )
    reward = feedback.get("reward") or {}
    return {
        "duplicate_poc": bool(feedback.get("duplicate_poc")),
        "new_runtime_evidence": bool(feedback.get("new_runtime_evidence")),
        "runtime_checked": bool(feedback.get("runtime_checked")),
        "trace_format": feedback.get("trace_format"),
        "diagnosis": feedback.get("diagnosis"),
        "declared_steps": feedback.get("declared_steps"),
        "observed_step_count": feedback.get("observed_steps"),
        "exactly_observed_step_count": feedback.get("exactly_observed_steps"),
        "stage_status": {
            "admission": reward.get("admission"),
            "root": reward.get("root"),
            "propagation": reward.get("propagation"),
            "target": reward.get("target"),
        },
        "first_unobserved_step": feedback.get("first_unobserved_step"),
        "first_out_of_order_step": (feedback.get("path") or {}).get(
            "first_out_of_order_step"
        ),
        "order_scope": (feedback.get("path") or {}).get("order_scope"),
        "state_summary": feedback.get("state"),
        "typed_runtime_relations": feedback.get("runtime_relations") or [],
        "runtime_call_observations": (
            feedback.get("runtime_call_observations") or []
        )[:32],
        "runtime_branch_observations": (
            feedback.get("runtime_branch_observations") or []
        )[:16],
        "candidate_verdict": feedback.get("candidate_verdict") or {},
        "step_evidence_semantics": {
            "scope": "bounded_checkpoint_observations_per_declared_trace_step",
            "program_execution_counts_available": False,
            "observed_sequence_is_complete_control_flow": False,
            "same_location_steps_are_independent_checkpoints": True,
        },
        "step_evidence": steps[:16],
    }


def validate_guidance(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("reward guidance is not an object")
    expected = {"last_confirmed", "first_failed", "reason"}
    if set(value) != expected:
        raise ValueError("runtime error report must contain exactly three fields")
    result: dict[str, str] = {}
    for name in ("last_confirmed", "first_failed", "reason"):
        item = value.get(name)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"reward guidance requires non-empty {name}")
        normalized = " ".join(item.split())
        if len(normalized) > 450:
            raise ValueError(f"reward guidance {name} exceeds 450 characters")
        result[name] = normalized
    return result


def call_lightweight_reward(
    *,
    issue_text: str,
    trace: list[dict[str, Any]],
    feedback: dict[str, Any],
    runtime_output: str,
    api_key: str,
    model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
    timeout: int = 90,
) -> dict[str, str]:
    request_context = {
        "public_issue": _trim(issue_text, 6000),
        "submitted_fine_trace": public_trace(trace),
        "deterministic_runtime_evidence": verifier_evidence(feedback),
        "candidate_runtime_output": _trim(runtime_output, 2400),
    }
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(request_context, ensure_ascii=False),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 500,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
        api_url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode())
    content = body["choices"][0]["message"]["content"]
    return validate_guidance(json.loads(content))
