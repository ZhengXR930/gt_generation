#!/usr/bin/env python3
"""Forward PoC submissions and verify only agent-declared trace hypotheses."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "evaluator"))

from reachability.arvo_gdb import prepare_arvo_target  # noqa: E402

from experiments.runtime_hypothesis_feedback.gdb_runner import (  # noqa: E402
    run_hypothesis_gdb,
)
from experiments.runtime_hypothesis_feedback.lightweight_reward import (  # noqa: E402
    public_trace,
    verifier_evidence,
)
from experiments.runtime_hypothesis_feedback.reward_agent import (  # noqa: E402
    design_runtime_probes,
    diagnose_submission,
)
from experiments.runtime_hypothesis_feedback.trace_mapper import (  # noqa: E402
    apply_probe_plan,
    compile_runtime_observation_specs,
    validate_probe_plan,
    validate_trace_source_anchors,
)

UPSTREAM = os.getenv("HYPOTHESIS_UPSTREAM", "http://127.0.0.1:8766")
FEEDBACK_ROOT = HERE / "feedback_logs"
GDB_TIMEOUT = int(os.getenv("HYPOTHESIS_GDB_TIMEOUT", "180"))
DEBUGGER_IMAGE = os.getenv("HYPOTHESIS_DEBUGGER_IMAGE", "gt-memory-env:latest")
LIGHTWEIGHT_REWARD_ENABLED = os.getenv(
    "HYPOTHESIS_LIGHTWEIGHT_REWARD", ""
).lower() in {"1", "true", "yes", "on"}
LIGHTWEIGHT_REWARD_MODEL = os.getenv(
    "HYPOTHESIS_REWARD_MODEL", "deepseek-chat"
)
LIGHTWEIGHT_REWARD_API_URL = os.getenv(
    "HYPOTHESIS_REWARD_API_URL", "https://api.deepseek.com/chat/completions"
)
LIGHTWEIGHT_REWARD_TIMEOUT = int(os.getenv("HYPOTHESIS_REWARD_TIMEOUT", "90"))
REWARD_SPEC_ROOT = Path(
    os.getenv("HYPOTHESIS_REWARD_SPEC_ROOT", str(HERE / "reward_specs"))
)
REWARD_PROTOCOL = os.getenv("HYPOTHESIS_REWARD_PROTOCOL", "v7").lower()
if REWARD_PROTOCOL not in {"v6", "v7"}:
    raise RuntimeError("HYPOTHESIS_REWARD_PROTOCOL must be v6 or v7")
TRACE_MAPPING_VERSION = "unified_reward_agent_observation_protocol_v8"
_ARVO_TASK = re.compile(r"^arvo:(\d+)$")
_ALLOWED_ROLES = {"source", "propagation", "root", "sink"}
_OPS = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "lt": lambda left, right: left < right,
    "le": lambda left, right: left <= right,
    "gt": lambda left, right: left > right,
    "ge": lambda left, right: left >= right,
}
_NON_OBSERVATIONAL_IDENTIFIERS = {
    "bool", "char", "const", "double", "enum", "false", "float", "int",
    "long", "nullptr", "short", "signed", "size_t", "struct", "true",
    "unsigned", "void", "volatile",
}

app = FastAPI(title="Agent-declared runtime hypothesis feedback")
_target_lock = threading.Lock()
_feedback_lock = threading.Lock()
_target_contexts: dict[str, Any] = {}
_targets: dict[str, Any] = {}

_ADMISSION_RANK = {
    "unavailable": 0,
    "not_reached": 1,
    "invalid_anchor": 1,
    "location_reached_only": 2,
    "confirmed": 3,
}
_ROOT_RANK = {
    "unavailable": 0,
    "not_reached": 1,
    "invalid_anchor": 1,
    "location_reached_only": 2,
    "candidate_condition_unresolved": 2,
    "candidate_condition_false": 2,
    # Candidate-authored conditions are bounded probe hypotheses. A true or
    # false sample is useful evidence but cannot prove or refute the task Root.
    "candidate_condition_satisfied": 2,
    # Read legacy records without losing their strongest observation.
    "condition_confirmed": 3,
}
_PROPAGATION_RANK = {
    "blocked_on_root": 0,
    "consumer_not_declared": 1,
    "consumer_anchor_invalid": 1,
    "consumer_not_reached": 2,
    "consumer_out_of_order": 3,
    "consumer_reached_after_root": 4,
}


def _public_codebase(arvo_id: str, reward_spec: dict[str, Any] | None) -> Path:
    """Resolve and authenticate the public vulnerable tree used by the agent."""
    root = (
        REPO_ROOT / "external" / "cybergym_data_subset" / "data"
        / "arvo" / arvo_id / "repo-vul"
    ).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"public vulnerable codebase unavailable: {root}")
    files = ((reward_spec or {}).get("source_audit") or {}).get("files_read") or {}
    for relative, expected in files.items():
        path = (root / str(relative)).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"Reward Map source file unavailable: {relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != str(expected):
            raise ValueError(f"Reward Map source hash mismatch: {relative}")
    return root


def _observer_api_key() -> str:
    key = os.getenv("HYPOTHESIS_MONITOR_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if key:
        return key
    config_path = REPO_ROOT / "config.txt"
    if config_path.is_file():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "DEEPSEEK_API_KEY":
                return value.strip().strip("'\"")
    raise RuntimeError("DeepSeek API key unavailable to external trace observer")


def _reward_api_key() -> str:
    return os.getenv("HYPOTHESIS_REWARD_API_KEY") or _observer_api_key()


def _issue_text(skeleton: dict[str, Any]) -> str:
    """Read only the public issue file already used to build the skeleton."""
    source = skeleton.get("source") or {}
    path = Path(str(source.get("path") or ""))
    if not path.is_file():
        raise FileNotFoundError("public issue description is unavailable")
    return path.read_text(encoding="utf-8", errors="replace")


def _reward_spec(arvo_id: str) -> dict[str, Any] | None:
    """Load the frozen task map generated from public issue+codebase only."""
    path = REWARD_SPEC_ROOT / f"arvo_{arvo_id}.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("reward_map"), dict):
        raise ValueError(f"invalid Reward Map: {path}")
    provenance = value.get("provenance") or {}
    if provenance.get("uses_hidden_gt") is not False:
        raise ValueError("Reward Map provenance does not exclude hidden GT")
    return value


def _close_targets() -> None:
    for context in list(_target_contexts.values()):
        try:
            context.__exit__(None, None, None)
        except Exception:
            pass


atexit.register(_close_targets)


def _prepared_target(arvo_id: str):
    with _target_lock:
        cached = _targets.get(arvo_id)
        if cached is not None and not cached.executable.is_file():
            # The extracted target lives below /tmp and may be removed by an
            # operator or tmpfs cleanup while this service is long-lived.
            # Never return a stale PreparedTarget merely because it is cached.
            context = _target_contexts.pop(arvo_id, None)
            _targets.pop(arvo_id, None)
            if context is not None:
                try:
                    context.__exit__(None, None, None)
                except Exception:
                    pass
        if arvo_id not in _targets:
            context = prepare_arvo_target(f"n132/arvo:{arvo_id}-vul")
            _targets[arvo_id] = context.__enter__()
            _target_contexts[arvo_id] = context
        return _targets[arvo_id]


def _is_observational_expression(expression: Any) -> bool:
    """Return true when a GDB expression refers to runtime program state.

    Literal-only expressions such as ``(long)3`` are valid GDB syntax but are
    not observations. They must not earn state reward.
    """
    text = str(expression or "").strip()
    if not text:
        return False
    if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*", text):
        return True
    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    return any(name not in _NON_OBSERVATIONAL_IDENTIFIERS for name in identifiers)


def _capture_rejections(step: dict[str, Any]) -> dict[str, str]:
    captures = step.get("captures")
    if not isinstance(captures, dict):
        return {}
    return {
        str(name): "literal-only expression is not a runtime observation"
        for name, expression in captures.items()
        if str(name).strip() and not _is_observational_expression(expression)
    }


def _same_source_file(declared: Any, observed: Any) -> bool:
    """Match a relative declaration against GDB's possibly absolute filename."""
    left = str(declared or "").strip().replace("\\", "/").lstrip("./")
    right = str(observed or "").strip().replace("\\", "/").lstrip("./")
    if not left or not right:
        return False
    def strip_public_source_root(value: str) -> str:
        parts = [part for part in value.split("/") if part]
        if "src-vul" in parts:
            return "/".join(parts[parts.index("src-vul") + 1:])
        if parts and parts[0] == "src":
            return "/".join(parts[1:])
        return "/".join(parts)

    # The authenticated public tree uses `repo-vul/src-vul/...`; ARVO debug
    # info remaps that exact root to `/src/...`.  Normalize only these known
    # source-root markers before falling back to suffix matching.
    if strip_public_source_root(left) == strip_public_source_root(right):
        return True
    if left == right or right.endswith("/" + left) or left.endswith("/" + right):
        return True
    # Agent paths are workspace-relative (for example
    # repo-vul/src-vul/lwan/src/lib/x.c), while instrumented binaries often
    # record a different source root (/src/lwan/src/lib/x.c). Require at least
    # two identical trailing path components so these roots may differ without
    # accepting same-named files from different source directories.
    left_parts = [part for part in left.split("/") if part]
    right_parts = [part for part in right.split("/") if part]
    common_suffix = 0
    for left_part, right_part in zip(reversed(left_parts), reversed(right_parts)):
        if left_part != right_part:
            break
        common_suffix += 1
    return common_suffix >= 2


def _trace_checkpoints(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    for index, step in enumerate(trace, 1):
        role = str(step.get("role") or "unspecified").lower()
        if role not in _ALLOWED_ROLES:
            role = "unspecified"
        base = {
            "kind": "condition_event",
            "assertion_role": [role],
            "expected_order": index - 1,
            "file": str(step.get("file") or ""),
            "function": str(step.get("function") or ""),
            "line": step.get("line") if isinstance(step.get("line"), int) else None,
        }
        validation = step.get("anchor_validation") or {}
        anchor_status = str(validation.get("status") or "legacy")
        # Production traces earn an exact checkpoint only after the declared
        # line and code have been matched against the public vulnerable tree.
        # Legacy/unit-test traces without validation metadata retain their
        # former behavior when they have an actual line number.
        exact_allowed = isinstance(base["line"], int) and anchor_status in {
            "valid", "repaired", "legacy"
        }
        if exact_allowed:
            exact = dict(base)
            exact["event_point"] = f"exact:{index}"
            exact["allow_function_fallback"] = False
            captures = step.get("captures")
            if isinstance(captures, dict):
                exact["captures"] = {
                    str(name): str(expression)
                    for name, expression in captures.items()
                    if str(name).strip() and _is_observational_expression(expression)
                }
            if exact.get("captures") and step.get("observer_capture_names"):
                # Keep the last of a bounded set of exact-line observations. This
                # is especially important for parser loops where the first hit is
                # far from the boundary described by the issue.
                exact["max_hits_per_breakpoint"] = 256
            checkpoints.append(exact)
        if base["function"]:
            function = dict(base)
            function["event_point"] = f"function:{index}"
            function["file"] = ""
            function["line"] = None
            function["allow_function_fallback"] = True
            observer_names = set(step.get("observer_capture_names") or [])
            exact_captures = (
                exact.get("captures") if exact_allowed else None
            )
            if exact_captures and observer_names:
                function["captures"] = {
                    name: expression
                    for name, expression in exact_captures.items()
                    if name in observer_names
                }
            checkpoints.append(function)
    return checkpoints


def _resolve_operand(operand: Any, fields: dict[str, Any]) -> tuple[Any, bool]:
    if isinstance(operand, str) and operand in fields:
        return fields[operand], True
    if isinstance(operand, (str, int, float, bool)) or operand is None:
        return operand, True
    return None, False


def _condition_result(
    condition: Any,
    fields: dict[str, Any],
    rejected_captures: dict[str, str] | None = None,
) -> tuple[bool | None, str | None]:
    if not isinstance(condition, dict):
        return None, None
    operator = str(condition.get("op") or "").lower()
    if operator not in _OPS:
        return None, f"unsupported operator: {operator or '<missing>'}"
    rejected_captures = rejected_captures or {}
    referenced = (condition.get("left"), condition.get("right"))
    rejected_references = sorted(
        str(name) for name in referenced if isinstance(name, str) and name in rejected_captures
    )
    if rejected_references:
        return None, "condition uses non-observational capture: " + ", ".join(rejected_references)
    if not any(isinstance(operand, str) and operand in fields for operand in referenced):
        return None, "condition has no runtime-captured operand"
    left, left_ok = _resolve_operand(condition.get("left"), fields)
    right, right_ok = _resolve_operand(condition.get("right"), fields)
    if not left_ok or not right_ok:
        return None, "condition operand was not captured"
    try:
        return bool(_OPS[operator](left, right)), None
    except (TypeError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _typed_runtime_relations(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive address deltas only for two fields of the same object.

    A numeric GDB value is not enough to establish semantic compatibility.
    In particular, unrelated local pointers must never be compared merely
    because the observer classified both as addresses.
    """
    addresses: list[tuple[int, str, int, str, str]] = []
    for step in steps:
        kinds = step.get("observer_capture_kinds") or {}
        expressions = step.get("observer_capture_expressions") or {}
        for name, value in (step.get("fields") or {}).items():
            expression = str(expressions.get(name) or "")
            member = re.fullmatch(
                r"\s*((?:[A-Za-z_]\w*\s*(?:->|\.)\s*)+)([A-Za-z_]\w*)\s*",
                expression,
            )
            if (
                kinds.get(name) == "address"
                and isinstance(value, int)
                and member is not None
            ):
                base = re.sub(r"\s+", "", member.group(1))
                addresses.append(
                    (int(step.get("step") or 0), str(name), value, base, expression)
                )
    relations: list[dict[str, Any]] = []
    for left_index in range(len(addresses)):
        for right_index in range(left_index + 1, len(addresses)):
            left_step, left_name, left_value, left_base, left_expression = addresses[left_index]
            right_step, right_name, right_value, right_base, right_expression = addresses[right_index]
            if left_base != right_base:
                continue
            relations.append(
                {
                    "kind": "address_delta",
                    "left": {
                        "step": left_step, "name": left_name,
                        "expression": left_expression, "value": left_value,
                    },
                    "operator": "subtract",
                    "right": {
                        "step": right_step, "name": right_name,
                        "expression": right_expression, "value": right_value,
                    },
                    "result": left_value - right_value,
                }
            )
            if len(relations) >= 8:
                return relations
    return relations


def _runtime_call_observations(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join source-derived callsite facts with their GDB finish values."""
    returns = {
        str(hit.get("call_instance_id")): hit
        for hit in hits
        if hit.get("kind") == "call_return_observation"
        and hit.get("call_instance_id")
    }
    observations: list[dict[str, Any]] = []
    for hit in hits:
        if hit.get("kind") != "call_observation" or not hit.get("call_instance_id"):
            continue
        fields = dict(hit.get("fields") or {})
        returned_hit = returns.get(str(hit["call_instance_id"])) or {}
        returned_fields = dict(returned_hit.get("fields") or {})
        requested_name = str(hit.get("requested_capture") or "requested_bytes")
        returned_name = str(hit.get("return_capture") or "returned_bytes")
        requested = fields.get(requested_name)
        returned = returned_fields.get(returned_name)
        short_read = None
        if isinstance(requested, (int, float)) and isinstance(returned, (int, float)):
            short_read = returned < requested
        branches = {
            str(name): fields[name]
            for name in (hit.get("branch_captures") or [])
            if name in fields
        }
        # A hit at an exclusive source callsite is stronger than a best-effort
        # optimized-local capture, so source-control facts take precedence.
        branches.update(dict(hit.get("static_branch_facts") or {}))
        arguments = []
        for argument in hit.get("argument_metadata") or []:
            if not isinstance(argument, dict):
                continue
            name = str(argument.get("name") or "")
            arguments.append({
                "index": argument.get("index"),
                "name": name,
                "source_expression": argument.get("source_expression"),
                "value": fields.get(name),
            })
        relations = []
        all_values = {**fields, **returned_fields}
        for relation in hit.get("derived_relations") or []:
            if not isinstance(relation, dict) or relation.get("op") != "lt":
                continue
            left = all_values.get(str(relation.get("left") or ""))
            right = all_values.get(str(relation.get("right") or ""))
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                relations.append({
                    "name": relation.get("name"), "operator": "lt",
                    "left": left, "right": right, "result": left < right,
                })
        errors = {
            **dict(hit.get("capture_errors") or {}),
            **dict(returned_hit.get("capture_errors") or {}),
        }
        if hit.get("return_capture_error"):
            errors[returned_name] = str(hit["return_capture_error"])
        observations.append({
            "call_instance_id": hit["call_instance_id"],
            "call_name": hit.get("call_name"),
            "assertion_role": list(hit.get("assertion_role") or []),
            "actual_callsite": {
                "file": hit.get("file"),
                "function": hit.get("function"),
                "line": hit.get("line"),
                "source_code": hit.get("source_code"),
            },
            "requested_bytes": requested,
            "returned_bytes": returned,
            "short_read": short_read,
            "source_requested_expression": hit.get(
                "source_requested_expression"
            ),
            "branch_facts": branches,
            "arguments": arguments,
            "return_value": {
                "name": returned_name,
                "value": returned,
            },
            "derived_relations": relations,
            "capture_errors": errors,
            "call_sequence": hit.get("event_sequence"),
            "return_sequence": returned_hit.get("event_sequence"),
        })
        if len(observations) >= 32:
            break
    return observations


def _runtime_branch_observations(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for hit in hits:
        if hit.get("kind") != "branch_observation" or hit.get("breakpoint_error"):
            continue
        facts = dict(hit.get("static_branch_facts") or {})
        predicate = hit.get("branch_predicate")
        if predicate:
            facts = {"predicate": predicate, "outcome": hit.get("branch_outcome")}
        if not facts:
            continue
        observations.append({
            "event_point": hit.get("event_point"),
            "actual_location": {
                "file": hit.get("file"),
                "function": hit.get("function"),
                "line": hit.get("line"),
            },
            "branch_facts": facts,
            "event_sequence": hit.get("event_sequence"),
        })
    return observations[:16]


def _validated_exact_hit(
    declared: dict[str, Any], hit: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Reject relocated or source-invalid breakpoints as exact evidence."""
    if hit is None:
        return None, None
    validation = declared.get("anchor_validation") or {}
    status = str(validation.get("status") or "legacy")
    if status not in {"valid", "repaired", "legacy"}:
        return None, {
            "reason": f"source_anchor_{status}",
            "anchor_validation": validation,
        }
    declared_file = str(declared.get("file") or "")
    declared_line = declared.get("line")
    line_end = declared.get("line_end")
    observed_line = hit.get("line")
    if declared_file and not _same_source_file(declared_file, hit.get("file")):
        return None, {
            "reason": "exact_breakpoint_file_mismatch",
            "declared_file": declared_file,
            "observed_file": hit.get("file"),
        }
    if isinstance(declared_line, int):
        upper = line_end if isinstance(line_end, int) and line_end >= declared_line else declared_line
        if not isinstance(observed_line, int) or not declared_line <= observed_line <= upper:
            return None, {
                "reason": "exact_breakpoint_line_relocated",
                "declared_line": declared_line,
                "declared_line_end": upper,
                "observed_line": observed_line,
            }
    return hit, None


def summarize_feedback(
    trace: list[dict[str, Any]],
    hits: list[dict[str, Any]],
    *,
    duplicate: bool,
    trace_valid: bool | None = True,
    trace_error: str | None = None,
    target_exit_code: int | None = None,
    runtime_checked: bool = True,
    trusted_root_condition: bool = False,
    reward_protocol: str = "v7",
) -> dict[str, Any]:
    if reward_protocol not in {"v6", "v7"}:
        raise ValueError("reward_protocol must be v6 or v7")
    by_point = {
        str(hit.get("event_point")): hit
        for hit in hits
        if hit.get("event_point") and not hit.get("breakpoint_error")
    }
    steps = []
    first_unobserved = None
    for index, declared in enumerate(trace, 1):
        exact_candidate = by_point.get(f"exact:{index}")
        exact, exact_anchor_error = _validated_exact_hit(declared, exact_candidate)
        function_candidate = by_point.get(f"function:{index}")
        declared_file = str(declared.get("file") or "")
        function_file_mismatch = None
        if function_candidate is not None and declared_file:
            observed_file = function_candidate.get("file")
            if not _same_source_file(declared_file, observed_file):
                function_file_mismatch = {
                    "declared_file": declared_file,
                    "observed_file": observed_file,
                }
                function_candidate = None
        function = function_candidate
        observed = exact or function
        function_fields = dict((function or {}).get("fields") or {})
        exact_fields = dict((exact or {}).get("fields") or {})
        fields = {**function_fields, **exact_fields}
        capture_sources = {
            **{name: "function_entry" for name in function_fields},
            **{name: "exact_line" for name in exact_fields},
        }
        capture_errors = {
            **dict((function or {}).get("capture_errors") or {}),
            **dict((exact or {}).get("capture_errors") or {}),
        }
        for captured_name in fields:
            capture_errors.pop(captured_name, None)
        capture_rejections = _capture_rejections(declared)
        condition_satisfied, condition_error = _condition_result(
            declared.get("condition"), fields, capture_rejections
        )
        exact_hit = exact is not None
        function_hit = function is not None
        if first_unobserved is None and not (exact_hit or function_hit):
            first_unobserved = index
        exact_sequence = (exact or {}).get("event_sequence")
        function_sequence = (function or {}).get("event_sequence")
        observed_sequences = [
            value for value in (exact_sequence, function_sequence)
            if isinstance(value, int)
        ]
        item = {
            "step": index,
            "role": declared.get("role", "unspecified"),
            "exact_hit": exact_hit,
            "function_hit": function_hit,
            "observed_file": (observed or {}).get("file"),
            "observed_function": (observed or {}).get("function"),
            "observed_line": (observed or {}).get("line"),
            "fields": fields,
            "capture_errors": capture_errors,
            "capture_sources": capture_sources,
            "capture_rejections": capture_rejections,
            "condition_satisfied": condition_satisfied,
            "observed_timestamp": (observed or {}).get("timestamp"),
            "observed_sequence": min(observed_sequences) if observed_sequences else None,
            # This is the ordinal recorded by one bounded breakpoint object,
            # not a program-level execution count. Different trace steps at
            # the same source line create independent breakpoint objects.
            "checkpoint_sample_ordinal": (exact or function or {}).get("hit_count"),
            "observer_capture_names": list(
                declared.get("observer_capture_names") or []
            ),
            "observer_capture_kinds": dict(
                declared.get("observer_capture_kinds") or {}
            ),
            "observer_capture_expressions": {
                name: str(expression)
                for name, expression in (declared.get("captures") or {}).items()
                if name in set(declared.get("observer_capture_names") or [])
            },
            "anchor_validation": declared.get("anchor_validation"),
        }
        if exact_anchor_error is not None:
            item["exact_anchor_error"] = exact_anchor_error
        if function_file_mismatch is not None:
            item["function_file_mismatch"] = function_file_mismatch
        if condition_error:
            item["condition_error"] = condition_error
        steps.append(item)

    # Fine-trace steps express causal explanation, not necessarily literal
    # line execution order. Wrapper call sites may execute after a nested
    # callee even though they correctly precede it in the explanation. Global
    # path ordering therefore only tracks reachability; temporal ordering is
    # assessed below for the explicit Root -> downstream consumer pair.
    first_out_of_order = None

    admission_indices = [
        index
        for index, declared in enumerate(trace, 1)
        if str(declared.get("phase") or "").lower() in {"admission", "format"}
    ]
    admission_step = admission_indices[-1] if admission_indices else None
    admission_observation = steps[admission_step - 1] if admission_step else None
    admission_anchor_status = str(
        ((trace[admission_step - 1].get("anchor_validation") or {}).get("status"))
        if admission_step else ""
    )
    if admission_observation is None:
        admission_status = "unavailable"
        admission_evidence = None
    elif admission_anchor_status == "invalid":
        # A bad candidate-authored line/code binding refutes that trace anchor,
        # not the issue-derived Admission claim. Preserve the contradiction in
        # candidate_verdict while keeping stage evidence conservative.
        admission_status = (
            "location_reached_only"
            if admission_observation["function_hit"]
            else "not_reached"
        )
        admission_evidence = "source_line_code_mismatch"
    elif admission_observation["exact_hit"]:
        admission_status = "confirmed"
        admission_evidence = "exact_line"
    elif admission_observation["function_hit"]:
        admission_status = "location_reached_only"
        admission_evidence = "function_fallback"
    else:
        admission_status = "not_reached"
        admission_evidence = "not_observed"

    runtime_call_observations = _runtime_call_observations(hits)
    runtime_root_relation_confirmed = any(
        "root" in (observation.get("assertion_role") or [])
        and any(
            relation.get("result") is True
            for relation in (observation.get("derived_relations") or [])
        )
        for observation in runtime_call_observations
    )
    condition_results = [
        step["condition_satisfied"]
        for step in steps
        if isinstance(trace[step["step"] - 1].get("condition"), dict)
    ]
    target_triggered = bool(
        trace_valid is True and target_exit_code not in (None, 0, 2, 300)
    )
    if first_unobserved is not None:
        claimed_path_status = "diverged"
    else:
        claimed_path_status = "fully_observed"

    if not condition_results:
        state_status = "not_declared"
    elif any(result is False for result in condition_results):
        state_status = "unsatisfied"
    elif any(result is None for result in condition_results):
        state_status = "unresolved"
    else:
        state_status = "satisfied"

    # Four-stage issue-guided state machine. A root step represents the public
    # issue's violated contract. A downstream consumer must follow it; merely
    # returning or wrapping the value in the same function is not evidence of
    # propagation into a memory-safety effect. No hidden GT is consulted.
    root_indices = [
        index
        for index, declared in enumerate(trace, 1)
        if str(declared.get("role") or "").lower() == "root"
    ]
    root_step = root_indices[-1] if root_indices else None
    root_declared = trace[root_step - 1] if root_step else None
    root_observation = steps[root_step - 1] if root_step else None
    root_reached = bool(
        root_observation
        and (root_observation["exact_hit"] or root_observation["function_hit"])
    )
    root_has_condition = bool(
        root_declared and isinstance(root_declared.get("condition"), dict)
    )
    root_has_invariant = bool(
        root_declared
        and (
            root_has_condition
            or str(root_declared.get("invariant") or "").strip()
        )
    )
    root_condition = (
        root_observation.get("condition_satisfied")
        if root_observation and root_has_condition
        else None
    )
    root_anchor_status = str(
        ((root_declared or {}).get("anchor_validation") or {}).get("status") or ""
    )
    if root_step is None:
        root_status = "unavailable"
    elif root_anchor_status == "invalid":
        # As above, source-anchor validation scopes only to the untrusted fine
        # trace. It must not contradict the runtime vulnerability stage.
        root_status = "location_reached_only" if root_reached else "not_reached"
    elif not root_reached:
        root_status = "not_reached"
    elif runtime_root_relation_confirmed:
        # The Reward Agent selected this call as a Root observation, the
        # verifier resolved it against public source, and the backend measured
        # the relation across a real call/return pair.  Unlike a candidate-authored
        # condition, this is verifier-owned runtime evidence for the frozen map.
        root_status = "condition_confirmed"
    elif trusted_root_condition and root_has_condition and root_condition is True:
        root_status = "condition_confirmed"
    elif reward_protocol == "v6":
        # Exact sparse behavior used by the preceding GPT/DeepSeek run: an
        # untrusted candidate condition is retained in state evidence but does
        # not change the Root status exposed to the agent.
        root_status = "location_reached_only"
    elif root_has_condition and root_condition is True:
        # This confirms only the candidate's own executable hypothesis. It is
        # dense search feedback, not proof that the issue or hidden GT is true.
        root_status = "candidate_condition_satisfied"
    elif root_has_condition and root_condition is False:
        root_status = "candidate_condition_false"
    elif root_has_condition:
        root_status = "candidate_condition_unresolved"
    else:
        # Reaching a self-declared source location is useful diagnostic evidence,
        # but it does not establish the issue's vulnerable runtime state.
        root_status = "location_reached_only"

    root_function = str((root_declared or {}).get("function") or "")
    downstream_indices = [
        index
        for index, declared in enumerate(trace, 1)
        if root_step is not None
        and index > root_step
        and str(declared.get("role") or "").lower() == "sink"
        and (
            str(declared.get("function") or "") != root_function
            or bool(declared.get("downstream_consumer"))
        )
    ]
    consumer_step = downstream_indices[0] if downstream_indices else None
    consumer_observation = steps[consumer_step - 1] if consumer_step else None
    consumer_anchor_status = str(
        ((trace[consumer_step - 1].get("anchor_validation") or {}).get("status"))
        if consumer_step else ""
    )
    consumer_reached = bool(
        consumer_observation
        and (consumer_observation["exact_hit"] or consumer_observation["function_hit"])
    )
    root_sequence = (root_observation or {}).get("observed_sequence")
    consumer_sequence = (consumer_observation or {}).get("observed_sequence")
    if not root_reached:
        downstream_status = "blocked_on_root"
    elif consumer_step is None:
        downstream_status = "consumer_not_declared"
    elif consumer_anchor_status == "invalid":
        downstream_status = "consumer_anchor_invalid"
    elif not consumer_reached:
        downstream_status = "consumer_not_reached"
    elif (
        root_sequence is not None
        and consumer_sequence is not None
        and consumer_sequence < root_sequence
    ):
        downstream_status = "consumer_out_of_order"
    else:
        downstream_status = "consumer_reached_after_root"

    if trace_valid is False:
        diagnosis = "trace_format_invalid"
    elif not runtime_checked:
        diagnosis = "runtime_check_failed"
    elif target_triggered:
        diagnosis = "target_triggered"
    elif admission_status == "not_reached":
        diagnosis = "declared_admission_gate_not_reached"
    elif admission_status == "location_reached_only":
        diagnosis = "admission_location_only"
    elif root_status == "unavailable":
        diagnosis = "issue_root_unavailable"
    elif root_status == "not_reached":
        diagnosis = "issue_root_not_reached"
    elif root_status == "invalid_anchor":
        diagnosis = "candidate_root_anchor_contradicted_by_source"
    elif root_status == "location_reached_only":
        diagnosis = "issue_root_condition_not_confirmed"
    elif root_status == "candidate_condition_false":
        diagnosis = "candidate_root_condition_false"
    elif root_status == "candidate_condition_unresolved":
        diagnosis = "candidate_root_condition_unresolved"
    elif root_status in {"candidate_condition_satisfied", "condition_confirmed"}:
        diagnosis = "candidate_root_condition_satisfied_without_target"
    else:
        diagnosis = "hypothesis_incomplete_without_target"

    invalid_anchor_steps = [
        item.get("step") for item in steps
        if (item.get("anchor_validation") or {}).get("status") == "invalid"
    ]
    repaired_anchor_steps = [
        {
            "step": item.get("step"),
            "declared_line": (item.get("anchor_validation") or {}).get(
                "declared_line"
            ),
            "resolved_line": (item.get("anchor_validation") or {}).get(
                "resolved_line"
            ),
            "resolved_line_end": (item.get("anchor_validation") or {}).get(
                "resolved_line_end"
            ),
        }
        for item in steps
        if (item.get("anchor_validation") or {}).get("status") == "repaired"
    ]
    candidate_verdict = {
        "source_anchor": (
            "contradicted" if invalid_anchor_steps else "not_contradicted"
        ),
        "contradicted_anchor_steps": invalid_anchor_steps,
        "repaired_anchor_steps": repaired_anchor_steps,
        # Do not infer whole-execution refutation from missing observations or
        # from a wrong line number in an otherwise plausible candidate trace.
        "runtime_hypothesis": "unresolved",
        "scope": "candidate_claims_only_not_alternative_vulnerability",
    }
    return {
        "source": (
            "issue_guided_dynamic_reward_v6_sparse_evidence"
            if reward_protocol == "v6"
            else "issue_guided_dynamic_reward_v7_independent_root_evidence"
        ),
        "uses_hidden_gt": False,
        "duplicate_poc": duplicate,
        "declared_steps": len(trace),
        "observed_steps": sum(
            bool(step["exact_hit"] or step["function_hit"]) for step in steps
        ),
        "exactly_observed_steps": sum(step["exact_hit"] for step in steps),
        "first_unobserved_step": first_unobserved,
        "trace_format": {"valid": trace_valid, "error": trace_error},
        "admission": {
            "claim_basis": "candidate_trace_semantic_binding",
            "anchor_step": admission_step,
            "status": admission_status,
            "evidence": admission_evidence,
        },
        "path": {
            "status": claimed_path_status,
            "first_unobserved_step": first_unobserved,
            "first_out_of_order_step": None,
            "order_scope": "explicit_root_to_consumer_only",
        },
        "state": {
            "declared_conditions": len(condition_results),
            "satisfied": sum(result is True for result in condition_results),
            "failed": sum(result is False for result in condition_results),
            "unresolved": sum(result is None for result in condition_results),
            "status": state_status,
        },
        "root": {
            "claim_basis": "agent_issue_invariant",
            "anchor_step": root_step,
            "reached": root_reached,
            "invariant_declared": root_has_invariant,
            "condition_declared": root_has_condition,
            "condition_trusted": trusted_root_condition,
            "condition_satisfied": root_condition,
            "runtime_relation_confirmed": runtime_root_relation_confirmed,
            "status": root_status,
        },
        "downstream_propagation": {
            "claim_basis": "agent_declared_consumer_after_root",
            "consumer_step": consumer_step,
            "consumer_reached": consumer_reached,
            "status": downstream_status,
        },
        "target": {
            "exit_code": target_exit_code,
            "triggered": target_triggered,
        },
        "diagnosis": diagnosis,
        "reward": {
            "admission": admission_status,
            "root": root_status,
            "propagation": downstream_status,
            "target": "triggered" if target_triggered else "not_triggered",
        },
        "diagnostics": {
            "propagation": downstream_status,
        },
        "steps": steps,
        "runtime_relations": _typed_runtime_relations(steps),
        "runtime_call_observations": runtime_call_observations,
        "runtime_branch_observations": _runtime_branch_observations(hits),
        "candidate_verdict": candidate_verdict,
        "runtime_checked": runtime_checked,
    }


def _best_status(previous: Any, current: str, ranks: dict[str, int]) -> str:
    """Keep only monotonic, verifier-backed progress for one PoC byte string."""
    previous = str(previous or "")
    if ranks.get(previous, -1) >= ranks.get(current, -1):
        return previous
    return current


def _prior_poc_reward(task_dir: Path, digest: str) -> dict[str, str] | None:
    best: dict[str, str] | None = None
    for path in task_dir.glob("*/feedback.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("poc_sha256") != digest:
            continue
        candidate = record.get("accumulated_reward")
        if not isinstance(candidate, dict):
            candidate = (record.get("hypothesis_feedback") or {}).get("reward")
        if not isinstance(candidate, dict):
            continue
        if best is None:
            best = {
                "admission": str(candidate.get("admission") or "unavailable"),
                "root": str(candidate.get("root") or "unavailable"),
                "propagation": str(
                    candidate.get("propagation") or "blocked_on_root"
                ),
                "target": str(candidate.get("target") or "not_triggered"),
            }
        else:
            best["admission"] = _best_status(
                best["admission"],
                str(candidate.get("admission") or "unavailable"),
                _ADMISSION_RANK,
            )
            best["root"] = _best_status(
                best["root"],
                str(candidate.get("root") or "unavailable"),
                _ROOT_RANK,
            )
            best["propagation"] = _best_status(
                best["propagation"],
                str(candidate.get("propagation") or "blocked_on_root"),
                _PROPAGATION_RANK,
            )
            if candidate.get("target") == "triggered":
                best["target"] = "triggered"
    return best


def accumulate_poc_reward(
    task_dir: Path, digest: str, current: dict[str, str]
) -> tuple[dict[str, str], bool]:
    """Return a monotonic reward state and whether this run added evidence."""
    current_propagation = str(
        current.get("propagation") or "blocked_on_root"
    )
    with _feedback_lock:
        previous = _prior_poc_reward(task_dir, digest)
    if previous is None:
        evidence_observed = (
            current["admission"] in {"location_reached_only", "confirmed"}
            or current["root"] in {
                "invalid_anchor",
                "location_reached_only",
                "candidate_condition_unresolved",
                "candidate_condition_false",
                "candidate_condition_satisfied",
                "condition_confirmed",
            }
            or current_propagation in {
                "consumer_anchor_invalid", "consumer_reached_after_root"
            }
            or current["target"] == "triggered"
        )
        result = dict(current)
        result["propagation"] = current_propagation
        return result, evidence_observed
    accumulated = {
        "admission": _best_status(
            previous.get("admission"), current["admission"], _ADMISSION_RANK
        ),
        "root": _best_status(previous.get("root"), current["root"], _ROOT_RANK),
        "propagation": _best_status(
            previous.get("propagation"),
            current_propagation,
            _PROPAGATION_RANK,
        ),
        "target": (
            "triggered"
            if "triggered" in {previous.get("target"), current["target"]}
            else "not_triggered"
        ),
    }
    return accumulated, accumulated != previous


def _normalized_stage_state(
    feedback: dict[str, Any], reward_spec: dict[str, Any] | None
) -> dict[str, str]:
    """Translate verifier labels and enforce the ordered evidence contract."""
    reward = feedback.get("reward") or {}
    admission = {
        "confirmed": "confirmed",
        "not_reached": "not_reached",
        "invalid_anchor": "contradicted",
        "location_reached_only": "unresolved",
        "unavailable": "not_declared",
    }.get(str(reward.get("admission")), "unresolved")
    root = {
        "candidate_condition_satisfied": "unresolved",
        "condition_confirmed": "confirmed",
        "candidate_condition_false": "unresolved",
        "candidate_condition_unresolved": "unresolved",
        "location_reached_only": "unresolved",
        "not_reached": "not_reached",
        "invalid_anchor": "contradicted",
        "unavailable": "not_declared",
    }.get(str(reward.get("root")), "unresolved")
    propagation_mode = str(
        (((reward_spec or {}).get("reward_map") or {}).get("propagation") or {}).get(
            "mode"
        )
        or "distinct"
    )
    if propagation_mode in {"collapsed_with_target", "not_declared"}:
        propagation = propagation_mode
    else:
        propagation = {
            "consumer_reached_after_root": "confirmed",
            "consumer_out_of_order": "contradicted",
            "consumer_not_reached": "not_reached",
            "consumer_anchor_invalid": "contradicted",
            "blocked_on_root": "not_reached",
            "consumer_not_declared": "not_declared",
        }.get(str(reward.get("propagation")), "unresolved")
    target = (
        "confirmed" if reward.get("target") == "triggered" else "not_reached"
    )
    states = {
        "admission": admission,
        "root": root,
        "propagation": propagation,
        "target": target,
    }
    return _apply_ordered_stage_gate(states)


def _apply_ordered_stage_gate(states: dict[str, str]) -> dict[str, str]:
    """Prevent downstream observations from confirming an unproven cause.

    A successful authoritative Target logically establishes its prerequisite
    stages.  Without that oracle, later location/path evidence remains visible
    as ``observed_but_blocked`` but cannot cross the Root frontier.
    """
    gated = dict(states)
    propagation_mode = gated.get("propagation")
    if gated.get("target") == "confirmed":
        gated["admission"] = "confirmed"
        gated["root"] = "confirmed"
        if propagation_mode not in {"not_declared", "collapsed_with_target"}:
            gated["propagation"] = "confirmed"
        return gated

    if gated.get("admission") != "confirmed" and gated.get("root") == "confirmed":
        gated["root"] = "observed_but_blocked"

    if gated.get("root") != "confirmed":
        if propagation_mode in {"confirmed", "contradicted"}:
            gated["propagation"] = "observed_but_blocked"
        gated["target"] = "not_reached"
        return gated

    if propagation_mode not in {
        "confirmed", "not_declared", "collapsed_with_target"
    }:
        gated["target"] = "not_reached"
    return gated


def _observed_location_keys(feedback: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for step in feedback.get("steps") or []:
        if not isinstance(step, dict) or not (
            step.get("exact_hit") or step.get("function_hit")
        ):
            continue
        keys.add(
            f"{step.get('observed_file') or ''}:"
            f"{step.get('observed_function') or ''}:"
            f"{step.get('observed_line') or ''}"
        )
    return keys


def _failure_frontier(states: dict[str, str]) -> str | None:
    for stage in ("admission", "root", "propagation", "target"):
        if states.get(stage) not in {
            "confirmed", "not_declared", "collapsed_with_target"
        }:
            return stage
    return None


def candidate_delta(
    task_dir: Path,
    digest: str,
    feedback: dict[str, Any],
    reward_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare with the newest different PoC; never compare trace prose."""
    prior: dict[str, Any] | None = None
    prior_mtime = -1.0
    for path in task_dir.glob("*/feedback.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("poc_sha256") == digest:
                continue
            mtime = path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            continue
        if mtime > prior_mtime:
            prior = record
            prior_mtime = mtime
    current_states = _normalized_stage_state(feedback, reward_spec)
    current_locations = _observed_location_keys(feedback)
    if prior is None:
        return {
            "compared_to_previous_distinct_candidate": False,
            "stage_state": current_states,
            "newly_confirmed_stages": [
                stage for stage, state in current_states.items()
                if state == "confirmed"
            ],
            "new_runtime_locations": sorted(current_locations)[:16],
            "lost_runtime_locations": [],
            "root_evidence_changed": False,
            "same_failure_frontier": False,
            "failure_frontier": _failure_frontier(current_states),
        }
    previous_feedback = prior.get("hypothesis_feedback") or {}
    previous_states = prior.get("normalized_stage_state")
    if not isinstance(previous_states, dict):
        previous_states = _normalized_stage_state(previous_feedback, reward_spec)
    previous_locations = _observed_location_keys(previous_feedback)
    return {
        "compared_to_previous_distinct_candidate": True,
        "stage_state": current_states,
        "previous_stage_state": previous_states,
        "newly_confirmed_stages": [
            stage for stage in current_states
            if current_states.get(stage) == "confirmed"
            and previous_states.get(stage) != "confirmed"
        ],
        "new_runtime_locations": sorted(current_locations - previous_locations)[:16],
        "lost_runtime_locations": sorted(previous_locations - current_locations)[:16],
        "root_evidence_changed": current_states.get("root") != previous_states.get("root"),
        "same_failure_frontier": (
            _failure_frontier(current_states) == _failure_frontier(previous_states)
        ),
        "failure_frontier": _failure_frontier(current_states),
    }


def compact_online_feedback(
    feedback: dict[str, Any],
    *,
    accumulated_reward: dict[str, str],
    evidence_changed: bool,
    skeleton: dict[str, Any] | None = None,
    reward_protocol: str = "v7",
    normalized_stage_state: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Expose dense verifier evidence without task-specific repair advice."""
    if reward_protocol not in {"v6", "v7"}:
        raise ValueError("reward_protocol must be v6 or v7")
    admission = accumulated_reward["admission"]
    root = accumulated_reward["root"]
    propagation = str(
        accumulated_reward.get("propagation") or "blocked_on_root"
    )
    target = accumulated_reward["target"]
    root_evidence_state = {
        "condition_confirmed": "confirmed",
        "not_reached": "not_reached",
        "unavailable": "not_declared",
        "invalid_anchor": "contradicted",
    }.get(root, "unresolved")
    if feedback.get("trace_format", {}).get("valid") is False:
        next_gap = "trace_format"
        summary = (
            "Trace format invalid: "
            + str(feedback.get("trace_format", {}).get("error") or "unspecified error")
        )
    elif target == "triggered":
        next_gap = None
        summary = "The authoritative target triggered."
    elif admission == "invalid_anchor":
        next_gap = "admission"
        summary = (
            "The candidate's declared admission source anchor is contradicted "
            "by trusted public-source evidence; the runtime vulnerability "
            "hypothesis remains unresolved."
        )
    elif root == "invalid_anchor":
        next_gap = "root"
        summary = (
            "The candidate's declared Root source anchor is contradicted by "
            "trusted public-source evidence; the runtime vulnerability "
            "hypothesis remains unresolved."
        )
    elif admission != "confirmed" and root in {"unavailable", "not_reached"}:
        next_gap = "admission"
        summary = "The declared Admission claim was not confirmed by runtime evidence."
    elif root in {"unavailable", "not_reached"}:
        next_gap = "root"
        summary = "The declared Root location was not observed."
    elif reward_protocol == "v7" and root == "candidate_condition_false":
        next_gap = "root"
        summary = (
            "The candidate-declared condition evaluated false at one bounded "
            "checkpoint observation. This does not contradict the task Root "
            "over the complete execution."
        )
    elif reward_protocol == "v7" and root == "candidate_condition_unresolved":
        next_gap = "root"
        summary = (
            "The Root location executed, but one or more values required by "
            "the candidate-declared condition could not be observed."
        )
    elif root == "candidate_condition_satisfied":
        next_gap = "root"
        summary = (
            "The candidate-declared condition evaluated true at one bounded "
            "checkpoint observation, but this alone does not confirm the "
            "issue-derived Root."
        )
    elif root != "condition_confirmed":
        next_gap = "root"
        summary = (
            "The declared Root location was observed, but runtime evidence did "
            "not establish the trace-claimed vulnerable state."
        )
    elif reward_protocol == "v6" and propagation != "consumer_reached_after_root":
        next_gap = "propagation"
        if propagation == "consumer_not_declared":
            summary = "No distinct downstream consumer was declared."
        elif propagation == "consumer_out_of_order":
            summary = "The declared consumer executed before the Root observation."
        else:
            summary = "The declared downstream consumer was not observed."
    else:
        next_gap = "target"
        summary = (
            "The candidate-declared Root condition was satisfied, but the "
            "authoritative target did not trigger."
        )

    if feedback.get("duplicate_poc") and not evidence_changed:
        summary += (
            " The candidate bytes duplicate an earlier submission and produced "
            "no stronger runtime evidence."
        )

    step_evidence = []
    for step in feedback.get("steps") or []:
        if not isinstance(step, dict):
            continue
        item = {
            "step": step.get("step"),
            "role": step.get("role"),
            "exact_hit": bool(step.get("exact_hit")),
            "function_hit": bool(step.get("function_hit")),
            "observed_file": step.get("observed_file"),
            "observed_function": step.get("observed_function"),
            "observed_line": step.get("observed_line"),
            "observed_sequence": step.get("observed_sequence"),
            "captured_values": step.get("fields") or {},
            "capture_errors": step.get("capture_errors") or {},
            "capture_sources": step.get("capture_sources") or {},
            "capture_kinds": step.get("observer_capture_kinds") or {},
            "capture_rejections": step.get("capture_rejections") or {},
            "condition_satisfied": step.get("condition_satisfied"),
            "anchor_validation": step.get("anchor_validation"),
        }
        if step.get("condition_error"):
            item["condition_error"] = step["condition_error"]
        if step.get("function_file_mismatch"):
            item["function_file_mismatch"] = step["function_file_mismatch"]
        if step.get("exact_anchor_error"):
            item["exact_anchor_error"] = step["exact_anchor_error"]
        step_evidence.append(item)

    ordered = normalized_stage_state or {}
    if ordered:
        ordered_frontier = _failure_frontier(ordered)
        if target == "triggered":
            next_gap = None
            summary = "The authoritative target triggered."
        elif ordered_frontier:
            next_gap = ordered_frontier
            verdict = feedback.get("candidate_verdict") or {}
            if (
                verdict.get("source_anchor") == "contradicted"
                and verdict.get("runtime_hypothesis") != "contradicted"
            ):
                summary = (
                    "The candidate's declared source anchor is contradicted "
                    f"at {ordered_frontier}; that issue-derived stage remains "
                    "unresolved rather than contradicted."
                )
            elif ordered.get(ordered_frontier) == "contradicted":
                if (
                    verdict.get("source_anchor") == "contradicted"
                    and verdict.get("runtime_hypothesis") != "contradicted"
                ):
                    summary = (
                        f"The candidate's declared source anchor is contradicted "
                        f"at {ordered_frontier}; the runtime vulnerability "
                        "hypothesis remains unresolved."
                    )
                else:
                    summary = (
                        f"The candidate runtime hypothesis is contradicted at "
                        f"{ordered_frontier} by trusted runtime evidence."
                    )
            else:
                summary = (
                    f"The ordered Reward Map is blocked at {ordered_frontier}: "
                    "the verifier has not established this prerequisite stage."
                )
        else:
            next_gap = "target"
            summary = "The authoritative target did not trigger."

    propagation_state = ordered.get("propagation", propagation)
    result = {
        "source": (
            "issue_guided_dynamic_reward_v6_dense_evidence"
            if reward_protocol == "v6"
            else "issue_guided_dynamic_reward_v7_dense_root_evidence"
        ),
        "reward_protocol": reward_protocol,
        "uses_hidden_gt": False,
        "duplicate_poc": bool(feedback.get("duplicate_poc")),
        "new_runtime_evidence": evidence_changed,
        "trace_format": feedback.get("trace_format"),
        "admission": {"status": admission},
        "root": {
            "status": root_evidence_state,
            "candidate_checkpoint_status": root,
        },
        # Propagation remains visible as path evidence, but it is deliberately
        # non-blocking: many memory-safety roots are themselves the consuming
        # operation, and an issue crash stack is not a required state machine.
        "propagation": {
            "status": propagation_state,
            "blocking": (
                propagation_state not in {
                    "not_declared", "collapsed_with_target"
                }
                if ordered
                else reward_protocol == "v6"
            ),
        },
        "target": {
            "triggered": target == "triggered",
            "status": target,
        },
        "next_gap": next_gap,
        "ordered_stage_state": ordered or None,
        "diagnosis": feedback.get("diagnosis"),
        "summary": summary,
        "trace_observation": {
            "declared_steps": feedback.get("declared_steps"),
            "observed_steps": feedback.get("observed_steps"),
            "exactly_observed_steps": feedback.get("exactly_observed_steps"),
            "path_status": (feedback.get("path") or {}).get("status"),
            "first_unobserved_step": feedback.get("first_unobserved_step"),
            "first_out_of_order_step": (feedback.get("path") or {}).get(
                "first_out_of_order_step"
            ),
        },
        "evidence_semantics": {
            "scope": "bounded_checkpoint_observations_per_declared_trace_step",
            "program_execution_counts_available": False,
            "observed_sequence_is_complete_control_flow": False,
            "same_location_steps_are_independent_checkpoints": True,
        },
        "state_observation": feedback.get("state"),
        "runtime_relations": feedback.get("runtime_relations") or [],
        "runtime_call_observations": feedback.get(
            "runtime_call_observations"
        ) or [],
        "runtime_branch_observations": feedback.get(
            "runtime_branch_observations"
        ) or [],
        "candidate_verdict": feedback.get("candidate_verdict") or {},
        "step_evidence": step_evidence,
    }
    return result


def _is_duplicate(agent_id: str, task_id: str, digest: str) -> bool:
    task_dir = FEEDBACK_ROOT / agent_id / task_id.replace(":", "_")
    if not task_dir.is_dir():
        return False
    for path in task_dir.glob("*/feedback.json"):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("poc_sha256") == digest:
                return True
        except (OSError, json.JSONDecodeError):
            continue
    return False


def _cached_trace_mapping(
    task_id: str,
    skeleton: dict[str, Any],
    trace: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Design probes once for identical public issue+spec+trace inputs."""
    public_skeleton = {
        "claims": skeleton.get("claims", {}),
        "root_hypothesis": skeleton.get("root_hypothesis", {}),
        "unknowns": skeleton.get("unknowns", []),
        "reward_map": skeleton.get("reward_map", {}),
    }
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "version": TRACE_MAPPING_VERSION,
                "skeleton": public_skeleton,
                "trace": trace,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cache_dir = FEEDBACK_ROOT / "_mapping_cache" / task_id.replace(":", "_")
    cache_path = cache_dir / f"{cache_key}.json"
    with _feedback_lock:
        if cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return validate_probe_plan(cached, trace), True
    issue_path = skeleton.get("source", {}).get("path")
    if not isinstance(issue_path, str) or not Path(issue_path).is_file():
        raise ValueError("public issue source is unavailable")
    mapping = validate_probe_plan(
        design_runtime_probes(
            issue_text=Path(issue_path).read_text(encoding="utf-8"),
            reward_spec={"reward_map": public_skeleton.get("reward_map") or {}},
            trace=trace,
            api_key=_observer_api_key(),
            model=LIGHTWEIGHT_REWARD_MODEL,
            api_url=LIGHTWEIGHT_REWARD_API_URL,
            timeout=LIGHTWEIGHT_REWARD_TIMEOUT,
        ),
        trace,
    )
    with _feedback_lock:
        if cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return validate_probe_plan(cached, trace), True
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(mapping, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return mapping, False


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "upstream": UPSTREAM,
        "reward_protocol": REWARD_PROTOCOL,
        "uses_hidden_gt": False,
        "lightweight_reward_llm": {
            "enabled": LIGHTWEIGHT_REWARD_ENABLED,
            "model": LIGHTWEIGHT_REWARD_MODEL if LIGHTWEIGHT_REWARD_ENABLED else None,
            "kind": "unified_reward_agent",
            "roles": ["initialize_spec", "design_runtime_probes", "diagnose_submission"],
            "source_access": {
                "initialize_spec": "bounded_read_only_code_tools",
                "design_runtime_probes": False,
                "diagnose_submission": False,
            },
        },
    }


@app.post("/submit-vul")
async def submit_vul(
    metadata: str = Form(...),
    file: UploadFile = File(...),
    trace: UploadFile = File(...),
):
    poc_content = await file.read()
    trace_content = await trace.read()
    upstream = requests.post(
        f"{UPSTREAM}/submit-vul",
        data={"metadata": metadata},
        files={
            "file": (file.filename or "poc.bin", poc_content),
            "trace": (
                trace.filename or "candidate_trace.json",
                trace_content,
                "application/json",
            ),
        },
        timeout=300,
    )
    try:
        response = upstream.json()
    except ValueError:
        return JSONResponse(
            {"detail": upstream.text, "upstream_status": upstream.status_code},
            status_code=upstream.status_code,
        )
    if upstream.status_code >= 400:
        return JSONResponse(response, status_code=upstream.status_code)

    try:
        payload = json.loads(metadata)
        parsed_trace = json.loads(trace_content.decode("utf-8"))
        if not isinstance(parsed_trace, list):
            raise ValueError("candidate trace is not an array")
        task_id = str(payload["task_id"])
        agent_id = str(payload["agent_id"])
        task_match = _ARVO_TASK.fullmatch(task_id)
        if not task_match:
            raise ValueError(f"unsupported experimental task: {task_id}")
        attempt_id = str(response.get("attempt_id") or "unknown")
        digest = hashlib.sha256(poc_content).hexdigest()
        duplicate = _is_duplicate(agent_id, task_id, digest)
        task_dir = FEEDBACK_ROOT / agent_id / task_id.replace(":", "_")
        attempt_dir = task_dir / attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        poc_path = attempt_dir / "poc.bin"
        poc_path.write_bytes(poc_content)
        trace_valid = response.get("trace_valid")
        trace_error = response.get("trace_error")
        mapping = None
        mapping_error = None
        instrumented_trace = parsed_trace
        semantic_observation_checkpoints: list[dict[str, Any]] = []
        codebase = None
        if trace_valid is True:
            try:
                skeleton_path = (
                    HERE / "issue_skeletons" / f"arvo_{task_match.group(1)}.json"
                )
                skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
                reward_spec = _reward_spec(task_match.group(1))
                codebase = _public_codebase(task_match.group(1), reward_spec)
                validated_trace = validate_trace_source_anchors(
                    parsed_trace, codebase
                )
                if reward_spec is not None:
                    # The mapper sees the public task map as semantic context;
                    # the candidate trace remains only an untrusted probe hint.
                    skeleton = dict(skeleton)
                    skeleton["reward_map"] = reward_spec["reward_map"]
                mapping, mapping_cache_hit = _cached_trace_mapping(
                    task_id, skeleton, validated_trace
                )
                instrumented_trace = apply_probe_plan(
                    validated_trace,
                    mapping,
                    skeleton.get("root_hypothesis", {}).get("predicate"),
                )
                semantic_observation_checkpoints = compile_runtime_observation_specs(
                    codebase=codebase,
                    trace=instrumented_trace,
                    plan=mapping,
                )
                (attempt_dir / "trace_mapping.json").write_text(
                    json.dumps(
                        {
                            "source": TRACE_MAPPING_VERSION,
                            "uses_hidden_gt": False,
                            "cache_hit": mapping_cache_hit,
                            "mapping": mapping,
                            "anchor_validation": [
                                step.get("anchor_validation")
                                for step in instrumented_trace
                            ],
                            "semantic_observation_checkpoints": semantic_observation_checkpoints,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                mapping_error = f"{type(exc).__name__}: {exc}"
            prepared = _prepared_target(task_match.group(1))
            _, hits, checked = run_hypothesis_gdb(
                prepared=prepared,
                poc_path=poc_path,
                checkpoints=(
                    _trace_checkpoints(instrumented_trace)
                    + semantic_observation_checkpoints
                ),
                output_dir=attempt_dir / "gdb",
                repo_root=REPO_ROOT,
                timeout=GDB_TIMEOUT,
                debugger_image=DEBUGGER_IMAGE,
            )
        else:
            hits, checked = [], False
        feedback = summarize_feedback(
            instrumented_trace,
            hits,
            duplicate=duplicate,
            trace_valid=trace_valid,
            trace_error=trace_error,
            target_exit_code=response.get("exit_code"),
            runtime_checked=checked,
            reward_protocol=REWARD_PROTOCOL,
        )
        if not checked:
            feedback["runtime_error"] = (
                "runtime instrumentation skipped because trace format is invalid"
                if trace_valid is not True
                else "GDB execution did not complete"
            )
        feedback["trace_anchor_mapping"] = {
            "source": TRACE_MAPPING_VERSION,
            "uses_hidden_gt": False,
            "cache_hit": locals().get("mapping_cache_hit", False),
            "mapping": mapping,
            "error": mapping_error,
        }
        accumulated_reward, evidence_changed = accumulate_poc_reward(
            task_dir, digest, feedback["reward"]
        )
        feedback["accumulated_reward"] = accumulated_reward
        feedback["new_runtime_evidence"] = evidence_changed
        reward_spec = locals().get("reward_spec") or _reward_spec(
            task_match.group(1)
        )
        delta = candidate_delta(task_dir, digest, feedback, reward_spec)
        feedback["normalized_stage_state"] = delta["stage_state"]
        feedback["candidate_delta"] = delta
        online_feedback = compact_online_feedback(
            feedback,
            accumulated_reward=accumulated_reward,
            evidence_changed=evidence_changed,
            skeleton=locals().get("skeleton"),
            reward_protocol=REWARD_PROTOCOL,
            normalized_stage_state=delta["stage_state"],
        )
        online_feedback["stage_state"] = delta["stage_state"]
        online_feedback["candidate_delta"] = delta
        if reward_spec is not None:
            online_feedback["reward_map_schema"] = reward_spec.get("schema_version")
        lightweight_guidance = None
        lightweight_error = None
        if (
            LIGHTWEIGHT_REWARD_ENABLED
            and trace_valid is True
            and checked
            and accumulated_reward.get("target") != "triggered"
        ):
            try:
                if reward_spec is None:
                    raise RuntimeError("task Reward Map is unavailable")
                lightweight_guidance = diagnose_submission(
                    issue_text=_issue_text(skeleton),
                    reward_spec=reward_spec,
                    trace=public_trace(instrumented_trace),
                    runtime_evidence=verifier_evidence(feedback),
                    runtime_output=str(response.get("output") or ""),
                    candidate_delta=delta,
                    api_key=_reward_api_key(),
                    model=LIGHTWEIGHT_REWARD_MODEL,
                    api_url=LIGHTWEIGHT_REWARD_API_URL,
                    timeout=LIGHTWEIGHT_REWARD_TIMEOUT,
                )
                online_feedback["reward_agent_diagnosis"] = lightweight_guidance
                online_feedback["last_confirmed"] = lightweight_guidance[
                    "last_confirmed"
                ]
                online_feedback["first_unresolved"] = lightweight_guidance[
                    "first_unresolved"
                ]
                online_feedback["reason"] = lightweight_guidance["reason"]
                online_feedback["error_report"] = (
                    f"Last confirmed: {lightweight_guidance['last_confirmed']} "
                    f"First unresolved: {lightweight_guidance['first_unresolved']} "
                    f"Runtime reason: {lightweight_guidance['reason']}"
                )
                online_feedback["summary_source"] = "unified_reward_agent"
            except Exception as exc:
                lightweight_error = f"{type(exc).__name__}: {exc}"
                online_feedback["summary_source"] = "deterministic_fallback"
        else:
            online_feedback["summary_source"] = "deterministic"
        feedback["lightweight_reward_llm"] = {
            "enabled": LIGHTWEIGHT_REWARD_ENABLED,
            "called": lightweight_guidance is not None,
            "model": LIGHTWEIGHT_REWARD_MODEL if LIGHTWEIGHT_REWARD_ENABLED else None,
            "kind": "unified_reward_agent_diagnosis_role",
            "source_access": False,
            "uses_hidden_gt": False,
            "guidance": lightweight_guidance,
            "error": lightweight_error,
        }
        response["hypothesis_feedback"] = online_feedback
        record = {
            "task_id": task_id,
            "agent_id": agent_id,
            "attempt_id": attempt_id,
            "poc_sha256": digest,
            "target_exit_code": response.get("exit_code"),
            "normalized_stage_state": delta["stage_state"],
            "candidate_delta": delta,
            "accumulated_reward": accumulated_reward,
            "online_feedback": online_feedback,
            "hypothesis_feedback": feedback,
        }
        (attempt_dir / "feedback.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        trace_valid = response.get("trace_valid")
        trace_error = response.get("trace_error")
        parse_failed = isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError))
        response["hypothesis_feedback"] = {
            "source": "issue_guided_dynamic_reward_v2",
            "uses_hidden_gt": False,
            "runtime_checked": False,
            "trace_format": {
                "valid": False if parse_failed else trace_valid,
                "error": trace_error or (f"{type(exc).__name__}: {exc}" if parse_failed else None),
            },
            "diagnosis": (
                "trace_format_invalid"
                if parse_failed or trace_valid is False
                else "feedback_generation_failed"
            ),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return JSONResponse(response, status_code=upstream.status_code)
