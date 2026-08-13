#!/usr/bin/env python3
"""Backfill joint analysis artifacts from frozen checkpoints.

This does not start an OpenHands runtime or replay a PoC.  It reconstructs the
model-visible conversation from the saved trajectory, removes any earlier fine
trace finalization turn, and performs one no-tools finalization conversation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.7
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "external" / "OpenHands"))

from evaluator.reasoning.analysis_artifact import (  # noqa: E402
    parse_analysis_artifact,
    validate_analysis_artifact,
    validate_analysis_artifact_quality,
)
from evaluator.reasoning.fine_trace import unwrap_final_answer_transport  # noqa: E402
from evaluator.reasoning.fine_trace import (  # noqa: E402
    parse_fine_trace,
    validate_fine_trace,
)
try:  # noqa: E402
    from openhands.agenthub.codeact_agent.codeact_agent import CodeActAgent
    from openhands.core.config import AgentConfig, LLMConfig
    from openhands.core.message import Message, TextContent
    from openhands.events.serialization.event import event_from_dict
    from openhands.llm.llm import LLM
except ModuleNotFoundError:  # pragma: no cover - depends on optional OpenHands env
    CodeActAgent = None
    AgentConfig = None
    LLMConfig = None
    Message = None
    TextContent = None
    event_from_dict = None
    LLM = None


DEFAULT_RESULTS = REPO_ROOT / "poc_generation" / "poc_results" / "deepseek-v4-flash"
FINALIZATION_MARKER = "[Fine Trace Finalization]"
OUTPUT_NAME = "analysis.json"
META_NAME = "analysis_generation.json"
OBSOLETE_TOP_LEVEL_OUTPUTS = (
    "analysis_artifact.json",
    "analysis_artifact.response.txt",
    "fine_trace.json",
    "fine_trace.response.txt",
    "semantic_claims.json",
    "semantic_claim_response.txt",
    "semantic_claim_generation.json",
    "vuln_logic.json",
)

SYSTEM_PROMPT = """You are an evaluation artifact finalizer. Tool use is disabled. Use only evidence already present in the conversation. Return exactly one bare JSON object with exactly three top-level keys: sample_id, fine_trace, and vuln_logic. Do not emit Markdown, prose, XML, DSML, tool calls, confidence fields, GT identifiers, or trace_step references.

Required JSON shape:
{
  "sample_id": "exact_sample_id",
  "fine_trace": [
    {
      "step": 1,
      "file": "project/source/file.c",
      "function": "function_name",
      "line": 123,
      "var": "source_expr",
      "code": "source statement",
      "role": "source",
      "note": "why this step matters"
    }
  ],
  "vuln_logic": {
    "source": {
      "file": "same file as the fine_trace source step",
      "function": "same function",
      "line": 123,
      "operands": ["attacker_controlled_expr"]
    },
    "root_cause": {
      "file": "same file as the fine_trace root_cause step",
      "function": "same function",
      "line": 130,
      "operands": ["left_expr", "right_expr"],
      "relation": {"op": "lt", "left": "left_expr", "right": "right_expr"}
    },
    "sink": {
      "file": "same file as the fine_trace sink step",
      "function": "same function",
      "line": 140,
      "operands": ["left_expr", "right_expr"],
      "relation": {"op": "gt", "left": "left_expr", "right": "right_expr"}
    },
    "propagation": [
      {
        "from": {"file": "file.c", "function": "f", "line": 123, "operands": ["expr"]},
        "to": {"file": "file.c", "function": "f", "line": 140, "operands": ["expr"]},
        "type": "data",
        "via": ["expr"],
        "relation": {"op": "eq", "left": "expr", "right": "expr"}
      }
    ]
  }
}

Field meanings:
- sample_id: the exact sample id provided in the final user prompt. Do not convert between arvo_123 and arvo:123.
- fine_trace: the shortest sufficient causal path through vulnerable project source code. Omit harness boilerplate, setup, generic parser admission, README/workspace artifacts, runtime logs, and incidental exploration.
- fine_trace.step: integer steps starting at 1 in causal/execution order.
- fine_trace.file/function/line: vulnerable project source location. line may be null only when the checkpoint evidence truly has no line, but any step used by vuln_logic must have an integer line.
- fine_trace.var: one concrete source expression, variable, field, macro, literal, or language-native variable token at that step.
- fine_trace.code: the source statement or a concise source-level description from the evidence.
- fine_trace.role: one of source, root_cause, sink, intermediate, or null. There must be exactly one source step, one root_cause step, and one sink step. Do not output depends_on.
- fine_trace.note: concise reason this step is on the causal path.

Role meanings:
- source: first vulnerable project source statement where attacker-controlled data or vulnerability-relevant state becomes a program value used by the real implementation. It is not a fuzz harness entrypoint, test driver, README, workspace setup, or generic parser admission unless that code is the vulnerable project implementation itself.
- root_cause: project source statement that represents the missing or violated safety obligation: pointer must be NULL after transfer, index < capacity, remaining bytes >= read size, object alive before use, buffer initialized before read, etc. It is not a symptom, crash line, generic error check, or harness line.
- sink: project source statement where the unsafe operation or vulnerability manifestation happens: out-of-bounds read/write, use-after-free, double free, invalid free, uninitialized read, null dereference, or overflow-triggering operation. It is not merely the final sanitizer stack frame if the actual unsafe project operation is visible elsewhere.
- intermediate: project source statement needed to carry data, control, object identity, lifetime, size, or ordering from source/root_cause to sink. Use null or omit role for ordinary nearby statements.

vuln_logic field meanings:
- vuln_logic is a projection from role-marked fine_trace steps, not a second independent story. If an anchor is wrong, fix the fine_trace role step first, then copy it into vuln_logic.
- source: copy file/function/line from the single fine_trace step with role=source. operands names the attacker-controlled value/object/size expression at that source. source has no relation or op.
- root_cause: copy file/function/line from the single fine_trace step with role=root_cause. operands are the concrete expressions involved in the violated safety obligation. relation is required and must be exactly {"op": "...", "left": "...", "right": "..."}.
- sink: copy file/function/line from the single fine_trace step with role=sink. operands are the concrete expressions involved in the unsafe operation or violated sink predicate. relation is required and must be exactly {"op": "...", "left": "...", "right": "..."}.
- propagation: each edge connects two existing fine_trace steps. from and to must copy file/function/line from existing fine_trace steps, usually source/root_cause/sink or intermediate. type is data, control, or order. via is the carrier expression, guard expression, or order keyword. relation is optional and, when present, must be exactly {"op": "...", "left": "...", "right": "..."}.
- Consistency: source/root_cause/sink operands and relation terms must be grounded in the same fine_trace step marked with that role. If vuln_logic.sink talks about glyph_props, the fine_trace sink step must also be the source statement involving glyph_props; do not put glyph_props under sink when the sink role step is a different call or variable.

Expression rules:
- relation.op must be one of eq, ne, lt, le, gt, ge, or same_object. Keep left/right direction meaningful for lt, le, gt, and ge.
- root_cause.relation and sink.relation must be the real safety condition or violated predicate. Do not use tautologies such as {"op":"eq","left":"x","right":"x"} or {"op":"same_object","left":"x","right":"x"} to fill the field.
- operands, via, relation.left, and relation.right must be concrete verbatim source expressions or literals from the cited source evidence: variables, fields, macros, constants, string/integer literals, calls, or language-native variables such as PHP $name tokens.
- Never put English explanations, conceptual phrases, unresolved instrumentation placeholders such as $event.field, or invented property names in operands, via, relation.left, or relation.right.
- README.md, workspace, checkpoint files, candidate_trace.json, analysis.json, prompts, runtime logs, harness, test, and fuzz setup code are not valid anchors for source, root_cause, sink, or propagation endpoints."""

USER_PROMPT = """[Analysis Artifact Finalization] Exploration is frozen and tools are unavailable. Based only on the checkpoint evidence, now return the fine_trace and vuln_logic together in the exact JSON object specified by the system message."""

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(?P<body>.*?)(?:\n```\s*)?$", re.DOTALL)
_LINE_NUMBER_RE = re.compile(r"(\d+)")
_SOURCE_LINE_RE = re.compile(
    r"(?P<file>src-vul/[^\s:\"']+\.(?:c|cc|cpp|h|hh|hpp)):"
    r"(?P<line>\d+):(?P<code>[^\n\r\"]{0,220})"
)


def _user_prompt_for_sample(sample_id: str) -> str:
    return (
        USER_PROMPT
        + "\nExpected sample_id: "
        + sample_id
        + "\nThe returned JSON object's sample_id field must exactly equal this "
        "value, byte-for-byte."
    )


def _parse_toml_value(value: str) -> Any:
    text = value.split("#", 1)[0].strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1]
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _loads_toml(text: str) -> dict[str, Any]:
    if tomllib is not None:
        return tomllib.loads(text)
    data: dict[str, Any] = {}
    current: dict[str, Any] = data
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = data
            for part in line[1:-1].split("."):
                current = current.setdefault(part.strip(), {})
            continue
        key, separator, value = line.partition("=")
        if separator:
            current[key.strip()] = _parse_toml_value(value)
    return data


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + "\n...[truncated middle]...\n" + text[-half:]


def _trajectory_text_context(sample_dir: Path, limit: int = 120000) -> tuple[str, dict[str, Any]]:
    events, total_events, was_truncated = _load_prefix(sample_dir / "checkpoint" / "trajectory")
    parts: list[str] = []
    raw_parts: list[str] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        action = str(item.get("action") or item.get("observation") or "")
        message = str(item.get("message") or "").strip()
        content = str(item.get("content") or "").strip()
        raw_parts.extend(part for part in (message, content) if part)
        if not message and not content:
            continue
        label = " ".join(part for part in (source, action) if part)
        parts.append(f"[{label}]\n{message}")
        if content and content != message:
            parts.append(f"[{label} content]\n{content}")
    source_line_context = _source_line_context(sample_dir, "\n".join(raw_parts))
    base_context = _truncate_text("\n\n".join(parts), limit)
    context = (source_line_context + "\n\n" + base_context).strip()
    return context, {
        "trajectory_events": total_events,
        "prefix_events": len(events),
        "removed_prior_finalization": was_truncated,
        "lightweight_context_chars": len(context),
    }


def _existing_trace_terms(sample_dir: Path) -> set[str]:
    terms: set[str] = set()
    path = sample_dir / OUTPUT_NAME
    if not path.is_file():
        return terms
    try:
        artifact = parse_analysis_artifact(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return terms
    trace = artifact.get("fine_trace") if isinstance(artifact, dict) else None
    if not isinstance(trace, list):
        return terms
    for step in trace:
        if not isinstance(step, dict):
            continue
        file_name = str(step.get("file") or "")
        if file_name:
            terms.add(file_name)
            terms.add(file_name.rsplit("/", 1)[-1])
            if file_name.startswith("src-vul/"):
                terms.add(file_name[len("src-vul/") :])
        function_name = str(step.get("function") or "")
        if function_name and function_name != "<global>":
            terms.add(function_name)
    return {term for term in terms if term}


def _source_line_context(sample_dir: Path, raw_text: str, limit: int = 16000) -> str:
    terms = _existing_trace_terms(sample_dir)
    filtered_matches: list[str] = []
    all_matches: list[str] = []
    seen: set[str] = set()
    for match in _SOURCE_LINE_RE.finditer(raw_text):
        file_name = match.group("file")
        code = match.group("code").strip()
        line = f"{file_name}:{match.group('line')}:{code}"
        if line in seen:
            continue
        seen.add(line)
        all_matches.append(line)
        if not terms or any(term in file_name or term in code for term in terms):
            filtered_matches.append(line)
        if len("\n".join(all_matches)) >= limit:
            break
    matches = filtered_matches or all_matches
    if not matches:
        return ""
    return (
        "[Checkpoint source-line evidence]\n"
        "The following vulnerable-source grep results appeared in the saved "
        "checkpoint and may be used for integer vuln_logic anchors:\n"
        + "\n".join(matches)
    )


def _chat_completion(
    *,
    api_key: str,
    model: str,
    base_url: str,
    messages: list[dict[str, str]],
    timeout: int = 180,
) -> str:
    import requests

    url = base_url
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model.replace("openai/", "", 1),
        "messages": messages,
    }
    if "modelhub" in base_url or model.startswith("gpt-5"):
        # ModelHub GPT-5.x rejects max_tokens and requires
        # max_completion_tokens.
        payload["max_completion_tokens"] = 8192
    else:
        payload["temperature"] = 0.0
        payload["max_tokens"] = 8192
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    return str(data["choices"][0]["message"].get("content") or "")


def _lightweight_artifact_backfill(
    sample_dir: Path,
    *,
    api_key: str,
    attempts: int,
    model: str,
    base_url: str,
    started: float,
) -> dict[str, Any]:
    context, source = _trajectory_text_context(sample_dir)
    existing_trace_context = ""
    existing_analysis_path = sample_dir / OUTPUT_NAME
    if existing_analysis_path.is_file():
        analysis_text = existing_analysis_path.read_text(encoding="utf-8")
        analysis = parse_analysis_artifact(analysis_text)
        trace = analysis.get("fine_trace") if isinstance(analysis, dict) else None
        quality_error = _artifact_error(analysis_text, sample_dir.name)
        if (
            not quality_error
            and isinstance(trace, list)
            and validate_fine_trace(json.dumps(trace)) is None
        ):
            existing_trace_context = (
                "\n\nAn earlier accepted analysis.json for this same checkpoint "
                "is included as evidence. You may reuse its fine_trace, but the "
                "final answer must still be one full analysis.json object:\n"
                + json.dumps(trace, ensure_ascii=False)
            )
    base_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                _user_prompt_for_sample(sample_dir.name)
                + "\n\nCheckpoint evidence follows. Use only this evidence.\n"
                + context
                + existing_trace_context
            ),
        },
    ]
    raw = ""
    error = "not attempted"
    for attempt in range(1, attempts + 1):
        messages = list(base_messages)
        if raw:
            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "The artifact was rejected because "
                            f"{error}. Return only the corrected bare JSON object "
                            "with exactly sample_id, fine_trace, and vuln_logic. "
                            "If the error names operands, via, relation.left, or "
                            "relation.right, replace that field with a concrete "
                            "source expression, literal, macro, or function-call "
                            "expression from the cited source evidence. Do not use "
                            "English explanatory phrases or placeholders such as "
                            "$attr. If the error says a line must be an integer, "
                            "replace null, unknown, or a range with the nearest "
                            "integer line number from the same vulnerable source "
                            "file/function in the checkpoint evidence. If the "
                            "error says relation is tautological or must describe "
                            "the violated safety condition, replace eq(x,x) or "
                            "same_object(x,x) with the actual required predicate "
                            "from the vulnerable source, such as index < capacity "
                            "or object != NULL before use. If the "
                            "error says operands/relation must be grounded in "
                            "the same fine_trace step, either move that role to "
                            "the trace step that actually contains those "
                            "expressions or change vuln_logic to use expressions "
                            "from the current role step. If the "
                            "error says vuln_logic must be projected from "
                            "fine_trace, update the corresponding role-marked "
                            "or intermediate fine_trace step and copy its "
                            "file/function/line into vuln_logic. If the error "
                            "mentions harness, test, fuzz, README, or workspace, "
                            "remove that key role and choose the first real "
                            "vulnerable project source statement for source, "
                            "the violated safety-obligation statement for "
                            "root_cause, and the unsafe operation statement for "
                            "sink."
                        ),
                    },
                ]
            )
        try:
            raw = _chat_completion(
                api_key=api_key,
                model=model,
                base_url=base_url,
                messages=messages,
            )
        except Exception as exc:
            raw = ""
            error = f"{type(exc).__name__}: {exc}"
            continue
        payload = _normalize_transport(raw)
        error = _artifact_error(payload, sample_dir.name)
        if not error:
            artifact = parse_analysis_artifact(payload)
            assert artifact is not None
            meta = {
                "status": "success",
                "method": "lightweight_checkpoint_analysis_artifact",
                "model": model,
                "attempts": attempt,
                "elapsed_seconds": round(time.time() - started, 3),
                **source,
            }
            _persist_success(sample_dir, artifact, raw, meta)
            return {"sample_id": sample_dir.name, **meta}
    meta = {
        "status": "invalid",
        "method": "lightweight_checkpoint_analysis_artifact",
        "model": model,
        "error": error,
        "attempts": attempts,
        "elapsed_seconds": round(time.time() - started, 3),
        **source,
    }
    _atomic_json(sample_dir / META_NAME, meta)
    return {"sample_id": sample_dir.name, **meta}


def _read_key(path: Path, name: str = "DEEPSEEK_API_KEY") -> str:
    if os.environ.get(name):
        return os.environ[name]
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip('"')
    raise RuntimeError(f"{name} is not available")


def _load_prefix(path: Path) -> tuple[list[Any], int, bool]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("trajectory is not a JSON array")
    cut = len(raw)
    for index, item in enumerate(raw):
        if FINALIZATION_MARKER in json.dumps(item, ensure_ascii=False):
            cut = index
            break
    if event_from_dict is None:
        events = raw[:cut]
    else:
        events = [event_from_dict(item) for item in raw[:cut]]
    if not events:
        raise ValueError("trajectory has no pre-finalization events")
    return events, len(raw), cut < len(raw)


def _llm_config(
    config_path: Path,
    api_key: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> LLMConfig:
    data = _loads_toml(config_path.read_text(encoding="utf-8"))
    llm_data = dict(data.get("llm") or {})
    if model:
        llm_data["model"] = model
    if base_url is not None:
        llm_data["base_url"] = base_url
    llm_data.update(
        api_key=api_key,
        temperature=0.0,
        max_output_tokens=max(8192, int(llm_data.get("max_output_tokens") or 0)),
        timeout=180,
        num_retries=4,
    )
    return LLMConfig.from_toml_section(llm_data)["llm"]


def _agent_config(config_path: Path) -> AgentConfig:
    data = _loads_toml(config_path.read_text(encoding="utf-8"))
    return AgentConfig.from_toml_section(data.get("agent") or {})["agent"]


def _messages(
    sample_dir: Path,
    api_key: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> tuple[LLM, list[Message], dict[str, Any]]:
    if any(item is None for item in (CodeActAgent, AgentConfig, LLMConfig, Message, TextContent, LLM)):
        raise RuntimeError("OpenHands Python dependencies are unavailable")
    checkpoint = sample_dir / "checkpoint"
    events, total_events, was_truncated = _load_prefix(checkpoint / "trajectory")
    llm = LLM(
        _llm_config(
            checkpoint / "config.toml",
            api_key,
            model=model,
            base_url=base_url,
        )
    )
    agent = CodeActAgent(llm, _agent_config(checkpoint / "config.toml"))
    messages = agent._get_messages(events)
    system = Message(
        role="system",
        content=[TextContent(text=SYSTEM_PROMPT)],
        force_string_serializer=True,
    )
    if messages and messages[0].role == "system":
        messages[0] = system
    else:
        messages.insert(0, system)
    messages.append(
        Message(
            role="user",
            content=[TextContent(text=_user_prompt_for_sample(sample_dir.name))],
            force_string_serializer=True,
        )
    )
    return llm, messages, {
        "trajectory_events": total_events,
        "prefix_events": len(events),
        "removed_prior_finalization": was_truncated,
    }


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _normalize_transport(response: str) -> str:
    """Remove only a whole-response DSML or JSON-fence transport wrapper."""
    payload = unwrap_final_answer_transport(response)
    match = _JSON_FENCE.fullmatch(payload)
    if match:
        candidate = match.group("body").strip()
        # A wrapper is transport, not permissive parsing: only remove it when
        # the complete inner value is already a schema-valid artifact.
        if validate_analysis_artifact(candidate) is None:
            return candidate
        repaired = _repair_artifact_payload(candidate)
        if validate_analysis_artifact(repaired) is None:
            return repaired
    repaired = _repair_artifact_payload(payload)
    if validate_analysis_artifact(repaired) is None:
        return repaired
    extracted = _extract_json_object(payload)
    if extracted is not None:
        repaired = _repair_artifact_payload(extracted)
        if validate_analysis_artifact(repaired) is None:
            return repaired
    return payload


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = text[start : end + 1].strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return json.dumps(value, ensure_ascii=False)


def _coerce_line(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = _LINE_NUMBER_RE.search(value)
        if match:
            return int(match.group(1))
    return value


def _trace_line_for_location(
    artifact: dict[str, Any],
    location: dict[str, Any],
    *,
    role: str | None = None,
) -> int | None:
    trace = artifact.get("fine_trace")
    if not isinstance(trace, list):
        return None
    file_name = str(location.get("file") or "")
    function_name = str(location.get("function") or "")

    def candidates(match_role: bool, match_function: bool, match_file: bool) -> list[int]:
        values: list[int] = []
        for step in trace:
            if not isinstance(step, dict) or not isinstance(step.get("line"), int):
                continue
            if match_role and role and step.get("role") != role:
                continue
            if match_function and function_name and step.get("function") != function_name:
                continue
            if match_file and file_name and step.get("file") != file_name:
                continue
            values.append(step["line"])
        return values

    for match_role, match_function, match_file in (
        (True, True, True),
        (False, True, True),
        (False, False, True),
    ):
        values = candidates(match_role, match_function, match_file)
        if values:
            return values[0]
    return None


def _coerce_location_line(
    artifact: dict[str, Any],
    location: dict[str, Any],
    *,
    role: str | None = None,
) -> None:
    if "line" not in location:
        return
    location["line"] = _coerce_line(location["line"])
    if not isinstance(location["line"], int):
        replacement = _trace_line_for_location(artifact, location, role=role)
        if replacement is not None:
            location["line"] = replacement


def _norm_repair_path(value: Any) -> str:
    path = str(value or "").replace("\\", "/").strip()
    for prefix in ("repo-vul/src-vul/", "src-vul/", "./"):
        while path.startswith(prefix):
            path = path[len(prefix):]
    return re.sub(r"/+", "/", path)


def _norm_repair_function(value: Any) -> str:
    text = str(value or "").strip().split("(", 1)[0].strip()
    parts = text.split()
    if parts:
        text = parts[-1]
    return re.sub(r"\s+", "", text)


def _same_repair_anchor(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_file = _norm_repair_path(left.get("file"))
    right_file = _norm_repair_path(right.get("file"))
    return (
        bool(left_file and right_file)
        and (left_file == right_file or left_file.endswith("/" + right_file) or right_file.endswith("/" + left_file))
        and _norm_repair_function(left.get("function")) == _norm_repair_function(right.get("function"))
        and left.get("line") == right.get("line")
        and isinstance(left.get("line"), int)
    )


def _project_logic_from_role_steps(artifact: dict[str, Any]) -> None:
    """Repair only schema projection, never semantic content."""
    trace = artifact.get("fine_trace")
    logic = artifact.get("vuln_logic")
    if not isinstance(trace, list) or not isinstance(logic, dict):
        return
    for role in ("source", "root_cause", "sink"):
        point = logic.get(role)
        if not isinstance(point, dict):
            continue
        role_steps = [
            step
            for step in trace
            if isinstance(step, dict) and step.get("role") == role
        ]
        if len(role_steps) != 1:
            continue
        step = role_steps[0]
        if _same_repair_anchor(step, point):
            continue
        for field in ("file", "function", "line"):
            if field in step:
                point[field] = step[field]


def _repair_artifact_payload(payload: str) -> str:
    artifact = parse_analysis_artifact(payload)
    if artifact is None:
        return payload

    for step in artifact.get("fine_trace") or []:
        if isinstance(step, dict) and "line" in step:
            step["line"] = _coerce_line(step["line"])

    logic = artifact.get("vuln_logic")
    if isinstance(logic, dict):
        expected_logic_keys = {"source", "root_cause", "sink", "propagation"}
        for key in list(logic):
            if key not in expected_logic_keys:
                logic.pop(key, None)
        logic.setdefault("propagation", [])
        for label in ("source", "root_cause", "sink"):
            value = logic.get(label)
            if isinstance(value, dict):
                _coerce_location_line(artifact, value, role=label)
        for edge in logic.get("propagation") or []:
            if not isinstance(edge, dict):
                continue
            for endpoint in ("from", "to"):
                value = edge.get(endpoint)
                if isinstance(value, dict):
                    _coerce_location_line(artifact, value)
        _project_logic_from_role_steps(artifact)

    return json.dumps(artifact, ensure_ascii=False)


def _artifact_error(payload: str, expected_sample_id: str) -> str:
    error = validate_analysis_artifact(payload)
    if error:
        return error
    artifact = parse_analysis_artifact(payload)
    if artifact is None:
        return "invalid JSON artifact"
    if artifact.get("sample_id") != expected_sample_id:
        return (
            "sample_id must exactly equal "
            f"{expected_sample_id!r}; do not rewrite separators or dataset prefixes"
        )
    quality_error = validate_analysis_artifact_quality(payload)
    if quality_error:
        return quality_error
    return ""


def _persist_success(
    sample_dir: Path,
    artifact: dict[str, Any],
    raw_response: str,
    meta: dict[str, Any],
) -> None:
    for name in OBSOLETE_TOP_LEVEL_OUTPUTS:
        _unlink_if_exists(sample_dir / name)
    _atomic_json(sample_dir / OUTPUT_NAME, artifact)
    _atomic_json(sample_dir / META_NAME, meta)


def _valid_existing(sample_dir: Path) -> bool:
    path = sample_dir / OUTPUT_NAME
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if _artifact_error(text, sample_dir.name) != "":
        return False
    artifact = parse_analysis_artifact(text)
    if artifact is None:
        return False
    return True


def generate_one(
    sample_dir: Path,
    api_key: str,
    attempts: int,
    overwrite: bool,
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    started = time.time()
    if not overwrite and _valid_existing(sample_dir):
        return {"sample_id": sample_dir.name, "status": "skipped_valid"}
    trajectory = sample_dir / "checkpoint" / "trajectory"
    config = sample_dir / "checkpoint" / "config.toml"
    if not trajectory.is_file() or not config.is_file():
        return {
            "sample_id": sample_dir.name,
            "status": "unavailable",
            "error": "checkpoint trajectory or config is missing",
        }
    if LLM is None:
        if not model or not base_url:
            return {
                "sample_id": sample_dir.name,
                "status": "unavailable",
                "error": "OpenHands dependencies unavailable; --model and --base-url are required for lightweight fallback",
            }
        return _lightweight_artifact_backfill(
            sample_dir,
            api_key=api_key,
            attempts=attempts,
            model=model,
            base_url=base_url,
            started=started,
        )

    try:
        repair_response = ""
        repair_error = ""
        skip_joint_retry = False
        previous_meta = sample_dir / META_NAME
        if previous_meta.is_file() and not overwrite:
            try:
                skip_joint_retry = (
                    json.loads(previous_meta.read_text(encoding="utf-8")).get("status")
                    == "invalid"
                )
            except (OSError, json.JSONDecodeError):
                pass
        llm, base_messages, source = _messages(
            sample_dir,
            api_key,
            model=model,
            base_url=base_url,
        )
        error = repair_error or "not attempted"
        raw_response = ""
        joint_attempts = 0 if skip_joint_retry else attempts
        for attempt in range(1, joint_attempts + 1):
            messages = list(base_messages)
            if repair_response:
                messages.append(
                    Message(
                        role="assistant",
                        content=[TextContent(text=repair_response)],
                        force_string_serializer=True,
                    )
                )
                messages.append(
                    Message(
                        role="user",
                        content=[
                            TextContent(
                                text=(
                                    "The previous response was rejected because "
                                    f"{error}. Return only a corrected bare JSON object "
                                    "with exactly sample_id, fine_trace, and vuln_logic. "
                                    "If the error names operands, via, relation.left, or "
                                    "relation.right, replace that field with a concrete "
                                    "source expression, literal, macro, or function-call "
                                    "expression from the cited source evidence. "
                                    "If the error says relation is tautological or "
                                    "must describe the violated safety condition, "
                                    "replace eq(x,x) or same_object(x,x) with the "
                                    "actual required predicate from the vulnerable "
                                    "source, such as index < capacity or object != "
                                    "NULL before use. "
                                    "If the error says operands/relation must be "
                                    "grounded in the same fine_trace step, either "
                                    "move that role to the trace step that actually "
                                    "contains those expressions or change vuln_logic "
                                    "to use expressions from the current role step. "
                                    "If the error says vuln_logic must be projected "
                                    "from fine_trace, update the corresponding "
                                    "role-marked or intermediate fine_trace step "
                                    "and copy its file/function/line into "
                                    "vuln_logic. If the error mentions harness, "
                                    "test, fuzz, README, or workspace, remove "
                                    "that key role and choose the first real "
                                    "vulnerable project source statement for "
                                    "source, the violated safety-obligation "
                                    "statement for root_cause, and the unsafe "
                                    "operation statement for sink. Do not surround it with "
                                    "backticks or a code fence."
                                )
                            )
                        ],
                        force_string_serializer=True,
                    )
                )
            response = llm.completion(
                messages=llm.format_messages_for_llm(messages),
                extra_body={
                    "metadata": {
                        "session_id": f"semantic-backfill-{sample_dir.name}",
                        "tags": ["artifact:joint-analysis", "model:deepseek"],
                    }
                },
            )
            raw_response = str(response.choices[0].message.content or "")
            payload = _normalize_transport(raw_response)
            error = _artifact_error(payload, sample_dir.name)
            if not error:
                artifact = parse_analysis_artifact(payload)
                assert artifact is not None
                meta = {
                    "status": "success",
                    "method": "checkpoint_joint_no_tools_finalization",
                    "model": llm.config.model,
                    "attempts": attempt,
                    "elapsed_seconds": round(time.time() - started, 3),
                    **source,
                }
                _persist_success(sample_dir, artifact, raw_response, meta)
                return {"sample_id": sample_dir.name, **meta}
            repair_response = raw_response

        meta = {
            "status": "invalid",
            "error": error,
            "attempts": joint_attempts,
            "elapsed_seconds": round(time.time() - started, 3),
            **source,
        }
        _atomic_json(sample_dir / META_NAME, meta)
        return {"sample_id": sample_dir.name, **meta}
    except Exception as exc:
        meta = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.time() - started, 3),
        }
        _atomic_json(sample_dir / META_NAME, meta)
        return {"sample_id": sample_dir.name, **meta}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument(
        "--sample-list",
        type=Path,
        help="newline-delimited sample ids to process",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.txt")
    parser.add_argument("--api-key-name", default="DEEPSEEK_API_KEY")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--progress", type=Path)
    args = parser.parse_args()

    results = args.results_dir.expanduser().resolve()
    sample_ids = list(args.sample_id)
    if args.sample_list:
        sample_ids.extend(
            line.strip()
            for line in args.sample_list.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    if sample_ids:
        samples = [results / sample_id for sample_id in sample_ids]
    else:
        samples = sorted(path.parent for path in results.glob("*/manifest.json"))
    if args.limit is not None:
        samples = samples[: args.limit]
    progress = args.progress or results / "analysis_artifact_backfill.jsonl"
    api_key = _read_key(args.config.expanduser().resolve(), args.api_key_name)
    lock = threading.Lock()
    counts: dict[str, int] = {}

    with progress.open("a", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    generate_one,
                    sample,
                    api_key,
                    args.attempts,
                    args.overwrite,
                    model=args.model,
                    base_url=args.base_url,
                ): sample.name
                for sample in samples
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "sample_id": futures[future],
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                status = str(result["status"])
                counts[status] = counts.get(status, 0) + 1
                with lock:
                    stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                    stream.flush()
                    print(
                        json.dumps(
                            {"completed": sum(counts.values()), "counts": counts, **result},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
    print(json.dumps({"total": len(samples), "counts": counts}, ensure_ascii=False))
    return 0 if not counts.get("error") and not counts.get("invalid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
