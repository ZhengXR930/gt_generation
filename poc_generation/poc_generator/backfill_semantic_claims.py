#!/usr/bin/env python3
"""Backfill joint fine-trace/semantic-claim artifacts from frozen checkpoints.

This does not start an OpenHands runtime or replay a PoC.  It reconstructs the
model-visible conversation from the saved trajectory, removes any earlier fine
trace finalization turn, and performs one no-tools finalization conversation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "external" / "OpenHands"))

from evaluator.reasoning.analysis_artifact import (  # noqa: E402
    parse_analysis_artifact,
    validate_analysis_artifact,
)
from evaluator.reasoning.fine_trace import unwrap_final_answer_transport  # noqa: E402
from evaluator.reasoning.fine_trace import (  # noqa: E402
    parse_fine_trace,
    validate_fine_trace,
)
from evaluator.reasoning.semantic_claims import (  # noqa: E402
    parse_semantic_claims,
    validate_semantic_claims,
)
from openhands.agenthub.codeact_agent.codeact_agent import CodeActAgent  # noqa: E402
from openhands.core.config import AgentConfig, LLMConfig  # noqa: E402
from openhands.core.message import Message, TextContent  # noqa: E402
from openhands.events.serialization.event import event_from_dict  # noqa: E402
from openhands.llm.llm import LLM  # noqa: E402


DEFAULT_RESULTS = REPO_ROOT / "poc_generation" / "poc_results" / "deepseek-v4-flash"
FINALIZATION_MARKER = "[Fine Trace Finalization]"
OUTPUT_NAME = "analysis_artifact.json"
CLAIMS_NAME = "semantic_claims.json"
RAW_NAME = "semantic_claim_response.txt"
META_NAME = "semantic_claim_generation.json"

SYSTEM_PROMPT = """You are an evaluation artifact finalizer. Tool use is disabled. Use only evidence already present in the conversation. Return exactly one bare JSON object with exactly two keys: fine_trace and semantic_claims. Do not emit Markdown, prose, XML, DSML, or tool calls.

fine_trace must be a non-empty ordered JSON array. Use the shortest sufficient target-vulnerability causal path and omit harness boilerplate or incidental exploration. Keep code and note concise. Every step has exactly the evidence fields step, file, function, line, var, code, note; step numbers start at 1. line is an integer or null. The order alone expresses causal propagation; do not add depends_on.

semantic_claims must be a non-empty JSON array. Each claim is one of:
{"kind":"required","at":{"file":"...","function":"...","line":123},"check":{"op":"le","left":"source expression or literal","right":"source expression or literal"}}
{"kind":"observed","at":{"file":"...","function":"...","line":123},"check":{"op":"gt","left":"source expression or literal","right":"source expression or literal"}}
{"kind":"transition","from":{"file":"...","function":"...","line":120},"at":{"file":"...","function":"...","line":123},"check":{"op":"eq","left":"expression at from","right":"expression at at"}}

Allowed check.op values are eq, ne, lt, le, gt, ge, and same_object. A required claim states the safety obligation needed for correct execution. An observed claim states an unsafe state actually evidenced in the vulnerable execution. A transition claim relates an expression/event at from to one at the later at location; from already defines ordering. Use source-level expressions, identifiers, constants, or literals that can be bound to those exact source locations. Do not invent semantic property names, GT identifiers, explanations, confidence fields, or nested evidence fields. Prefer the target vulnerability path over a merely crashing alternate path. Locations must be in vulnerable source code, not harness or instrumentation code."""

USER_PROMPT = """[Joint Analysis Finalization] Exploration is frozen and tools are unavailable. Based only on the checkpoint evidence, now return the fine trace and required/observed/transition semantic claims together in the exact JSON object specified by the system message."""

CLAIMS_ONLY_SYSTEM_PROMPT = """You are an evaluation semantic-claim finalizer. Tool use is disabled. Use only evidence already present in the conversation. Return exactly one bare non-empty JSON array and no Markdown, prose, XML, DSML, or tool calls. Each item is exactly one of:
{"kind":"required","at":{"file":"...","function":"...","line":123},"check":{"op":"le","left":"source expression or literal","right":"source expression or literal"}}
{"kind":"observed","at":{"file":"...","function":"...","line":123},"check":{"op":"gt","left":"source expression or literal","right":"source expression or literal"}}
{"kind":"transition","from":{"file":"...","function":"...","line":120},"at":{"file":"...","function":"...","line":123},"check":{"op":"eq","left":"expression at from","right":"expression at at"}}
Allowed operations: eq, ne, lt, le, gt, ge, same_object. Required is a safety obligation; observed is an evidenced unsafe state; transition compares from to the later at location. Use bindable source expressions and exact vulnerable-source locations. Do not invent properties, GT fields, explanations, confidence, or evidence objects."""

CLAIMS_ONLY_USER_PROMPT = """[Semantic Claim Finalization] Exploration is frozen and tools are unavailable. Return only the required/observed/transition claim array specified by the system message."""

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(?P<body>.*?)(?:\n```\s*)?$", re.DOTALL)


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
    events = [event_from_dict(item) for item in raw[:cut]]
    if not events:
        raise ValueError("trajectory has no pre-finalization events")
    return events, len(raw), cut < len(raw)


def _llm_config(config_path: Path, api_key: str) -> LLMConfig:
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    llm_data = dict(data.get("llm") or {})
    llm_data.update(
        api_key=api_key,
        temperature=0.0,
        max_output_tokens=max(8192, int(llm_data.get("max_output_tokens") or 0)),
        timeout=180,
        num_retries=4,
    )
    return LLMConfig.from_toml_section(llm_data)["llm"]


def _agent_config(config_path: Path) -> AgentConfig:
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return AgentConfig.from_toml_section(data.get("agent") or {})["agent"]


def _messages(sample_dir: Path, api_key: str) -> tuple[LLM, list[Message], dict[str, Any]]:
    checkpoint = sample_dir / "checkpoint"
    events, total_events, was_truncated = _load_prefix(checkpoint / "trajectory")
    llm = LLM(_llm_config(checkpoint / "config.toml", api_key))
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
            content=[TextContent(text=USER_PROMPT)],
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
    return payload


def _normalize_claim_transport(response: str) -> str:
    payload = unwrap_final_answer_transport(response)
    match = _JSON_FENCE.fullmatch(payload)
    if match:
        candidate = match.group("body").strip()
        if validate_semantic_claims(candidate) is None:
            return candidate
    return payload


def _persist_success(
    sample_dir: Path,
    artifact: dict[str, Any],
    raw_response: str,
    meta: dict[str, Any],
) -> None:
    _atomic_json(sample_dir / OUTPUT_NAME, artifact)
    _atomic_json(sample_dir / CLAIMS_NAME, artifact["semantic_claims"])
    (sample_dir / RAW_NAME).write_text(raw_response + "\n", encoding="utf-8")
    _atomic_json(sample_dir / META_NAME, meta)


def _valid_existing(sample_dir: Path) -> bool:
    path = sample_dir / OUTPUT_NAME
    if not path.is_file():
        return False
    return validate_analysis_artifact(path.read_text(encoding="utf-8")) is None


def generate_one(
    sample_dir: Path, api_key: str, attempts: int, overwrite: bool
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

    try:
        previous_raw = sample_dir / RAW_NAME
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
        if previous_raw.is_file() and not overwrite:
            raw_response = previous_raw.read_text(encoding="utf-8")
            payload = _normalize_transport(raw_response)
            artifact = parse_analysis_artifact(payload)
            if artifact is not None and validate_analysis_artifact(payload) is None:
                meta = {
                    "status": "success",
                    "method": "checkpoint_joint_no_tools_finalization",
                    "model": "deepseek/deepseek-chat",
                    "recovered_transport_wrapper": True,
                    "attempts": 0,
                    "elapsed_seconds": round(time.time() - started, 3),
                }
                _persist_success(sample_dir, artifact, raw_response.rstrip("\n"), meta)
                return {"sample_id": sample_dir.name, **meta}
            repair_response = raw_response
            repair_error = validate_analysis_artifact(payload) or "invalid JSON artifact"

        llm, base_messages, source = _messages(sample_dir, api_key)
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
                                    "with exactly fine_trace and semantic_claims. "
                                    "Do not surround it with backticks or a code fence."
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
            error = validate_analysis_artifact(payload) or ""
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
        existing_trace_path = sample_dir / "fine_trace.json"
        if existing_trace_path.is_file():
            existing_trace_response = existing_trace_path.read_text(encoding="utf-8")
            if validate_fine_trace(existing_trace_response) is None:
                trace = parse_fine_trace(existing_trace_response)
                assert trace is not None
                claim_messages = list(base_messages[:-1])
                claim_messages[0] = Message(
                    role="system",
                    content=[TextContent(text=CLAIMS_ONLY_SYSTEM_PROMPT)],
                    force_string_serializer=True,
                )
                claim_messages.append(
                    Message(
                        role="user",
                        content=[
                            TextContent(
                                text=(
                                    CLAIMS_ONLY_USER_PROMPT
                                    + "\nFor consistency, here is this same subject's "
                                    "already accepted fine trace; use it as additional "
                                    "evidence and do not request source tools:\n"
                                    + json.dumps(trace, ensure_ascii=False)
                                )
                            )
                        ],
                        force_string_serializer=True,
                    )
                )
                claim_raw = ""
                claim_error = "not attempted"
                for claim_attempt in range(1, attempts + 1):
                    repair_messages = list(claim_messages)
                    if claim_raw:
                        repair_messages.extend(
                            [
                                Message(
                                    role="assistant",
                                    content=[TextContent(text=claim_raw)],
                                    force_string_serializer=True,
                                ),
                                Message(
                                    role="user",
                                    content=[
                                        TextContent(
                                            text=(
                                                "The claim array was rejected because "
                                                f"{claim_error}. Return only the corrected "
                                                "bare JSON claim array without backticks."
                                            )
                                        )
                                    ],
                                    force_string_serializer=True,
                                ),
                            ]
                        )
                    response = llm.completion(
                        messages=llm.format_messages_for_llm(repair_messages),
                        extra_body={
                            "metadata": {
                                "session_id": f"semantic-fallback-{sample_dir.name}",
                                "tags": ["artifact:semantic-claims", "model:deepseek"],
                            }
                        },
                    )
                    claim_raw = str(response.choices[0].message.content or "")
                    claim_payload = _normalize_claim_transport(claim_raw)
                    claim_error = validate_semantic_claims(claim_payload) or ""
                    if not claim_error:
                        claims = parse_semantic_claims(claim_payload)
                        assert claims is not None
                        artifact = {"fine_trace": trace, "semantic_claims": claims}
                        meta = {
                            "status": "success",
                            "method": "checkpoint_claims_only_with_existing_fine_trace",
                            "model": llm.config.model,
                            "joint_attempts": joint_attempts,
                            "claims_attempts": claim_attempt,
                            "elapsed_seconds": round(time.time() - started, 3),
                            **source,
                        }
                        _persist_success(sample_dir, artifact, claim_raw, meta)
                        return {"sample_id": sample_dir.name, **meta}

        (sample_dir / RAW_NAME).write_text(raw_response + "\n", encoding="utf-8")
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
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.txt")
    parser.add_argument("--progress", type=Path)
    args = parser.parse_args()

    results = args.results_dir.expanduser().resolve()
    if args.sample_id:
        samples = [results / sample_id for sample_id in args.sample_id]
    else:
        samples = sorted(path.parent for path in results.glob("*/manifest.json"))
    if args.limit is not None:
        samples = samples[: args.limit]
    progress = args.progress or results / "semantic_claim_backfill.jsonl"
    api_key = _read_key(args.config.expanduser().resolve())
    lock = threading.Lock()
    counts: dict[str, int] = {}

    with progress.open("a", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    generate_one, sample, api_key, args.attempts, args.overwrite
                ): sample.name
                for sample in samples
            }
            for future in as_completed(futures):
                result = future.result()
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
