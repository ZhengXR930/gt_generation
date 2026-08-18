#!/usr/bin/env python3
"""No-tool DeepSeek finalization for missing DSH analysis.json artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests


HERE = Path(__file__).resolve().parent
GENERATOR_ROOT = HERE.parent
GT_ROOT = GENERATOR_ROOT.parents[1]
RESULTS_ROOT = GENERATOR_ROOT.parent / "poc_results"

sys.path.insert(0, str(GENERATOR_ROOT))
sys.path.insert(0, str(GT_ROOT))

from evaluator.reasoning.analysis_artifact import validate_analysis_artifact_quality  # noqa: E402
from openhands_backend.run_sample import load_env_key  # noqa: E402


_LINE_NUMBER_RE = re.compile(r"(\d+)")
_SOURCE_EXPR_RE = re.compile(
    r"(->|::|[A-Za-z_][A-Za-z0-9_]*\s*\(|\[[^\]]*\]|"
    r"[().*&=<>!+\-/%|^~?:,]|^[-+]?(?:0x[0-9A-Fa-f]+|\d+)(?:[uUlLfF]*)$|"
    r"^\".*\"$|^'.*'$)"
)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REL_OPS = {"eq", "ne", "lt", "le", "gt", "ge", "same_object"}


SCHEMA = """Return exactly one JSON object with exactly these top-level keys:
sample_id, fine_trace, vuln_logic.

fine_trace: ordered project-source causal path. Use 3 to 8 steps only:
source, root_cause, sink, plus only indispensable intermediate steps. Do not
expand loops, repeated call chains, or every observed action. Each step has:
step:int, file:string, function:string, line:int|null, var:string, code:string,
note:string, role:"source"|"root_cause"|"sink"|"intermediate"|null.
Do not output depends_on.

Mark exactly one source, one root_cause, and one sink step.

vuln_logic:
- source/root_cause/sink copy file/function/line from the matching role-marked
  fine_trace step and include operands: non-empty string array. In vuln_logic,
  line must be an integer.
- root_cause and sink include relation exactly {"op":"...","left":"...","right":"..."}.
  op is one of eq, ne, lt, le, gt, ge, same_object.
- propagation is an array of edges. Each edge contains from, to, type, via,
  optional relation. from/to copy file/function/line from existing fine_trace
  steps and each from/to endpoint must include operands: non-empty string array.
  type is data, control, or order. via is non-empty string array.

Use vulnerable project source only. Do not cite README, analysis files,
checkpoint files, runtime logs, harness/test/fuzz setup, or old results.
"""


def failed_samples_from_summary(summary_path: Path) -> list[str]:
    samples: list[str] = []
    for line in summary_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "failed":
            sample = str(record.get("sample") or "")
            if sample:
                samples.append(sample)
    return samples


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def event_blocks(sample_dir: Path, *, max_events: int, max_chars: int) -> str:
    session_files = sorted((sample_dir / "checkpoint" / "dsh_home" / "sessions-jsonl").glob("**/session.jsonl"))
    blocks: list[str] = []
    for session_file in session_files:
        for line in session_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if event_type == "assistant/message":
                message = data.get("message") if isinstance(data.get("message"), dict) else {}
                texts: list[str] = []
                for item in message.get("content") or []:
                    if isinstance(item, dict) and item.get("type") in {"text", "reasoning"}:
                        text = str(item.get("text") or "").strip()
                        if text:
                            texts.append(text)
                if texts:
                    blocks.append(f"ASSISTANT step {data.get('step')}:\n" + "\n".join(texts)[-1800:])
            elif event_type == "tool/call":
                name = data.get("name")
                args = str(data.get("arguments") or "")
                if name in {"read", "grep", "bash"}:
                    blocks.append(f"TOOL CALL step {data.get('step')} {name}:\n{args[:900]}")
            elif event_type == "tool/result":
                text_parts: list[str] = []
                message = data.get("message") if isinstance(data.get("message"), dict) else {}
                for item in message.get("content") or []:
                    if not isinstance(item, dict):
                        continue
                    for inner in item.get("content") or []:
                        if isinstance(inner, dict) and inner.get("type") == "text":
                            text_parts.append(str(inner.get("text") or ""))
                if text_parts:
                    text = "\n".join(text_parts)
                    # Keep the beginning for line-numbered reads and the tail for
                    # command failures/grep summaries.
                    if len(text) > 2200:
                        text = text[:1200] + "\n...\n" + text[-900:]
                    blocks.append(f"TOOL RESULT step {data.get('step')}:\n{text}")
    rendered = "\n\n".join(blocks[-max_events:])
    if len(rendered) > max_chars:
        rendered = rendered[-max_chars:]
    return rendered


def build_prompt(sample: str, sample_dir: Path, *, max_events: int, max_chars: int) -> str:
    arvo_id = sample.split("_", 1)[1]
    source_root = GT_ROOT / "external" / "cybergym_data_subset" / "data" / "arvo" / arvo_id
    description = (source_root / "description.txt").read_text(encoding="utf-8", errors="replace")
    evidence = event_blocks(sample_dir, max_events=max_events, max_chars=max_chars)
    manifest_path = sample_dir / "manifest.json"
    run_status = "unknown"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            run_status = str(manifest.get("status") or manifest.get("final_status") or "unknown")
        except Exception:
            run_status = "unknown"
    return f"""You are finalizing the output of a timed-out PoC generation agent.

The original PoC generation run ended with status: {run_status}. Its saved
analysis is missing or does not satisfy the current required schema. You are
not judging the agent and you must not propose new PoC construction steps.
Your task is only to convert the saved reasoning trajectory into the required
structured analysis artifact.

Trusted issue description:
{description.strip()}

Saved trajectory evidence:
{evidence.strip()}

Rules:
- Output one bare JSON object only. No markdown, no prose.
- sample_id must be "{sample}".
- If the trajectory is uncertain, choose the most concrete vulnerability
  hypothesis supported by the issue description and cited source snippets.
- Keep operands/relation grounded in the same fine_trace step's var/code.
- Keep fine_trace compact: 3 to 8 steps total. Never output more than 8 steps.

{SCHEMA}
"""


def call_deepseek(
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    *,
    max_tokens: int,
    reasoning_effort: str,
) -> tuple[str, dict[str, Any]]:
    url = (base_url.rstrip("/") + "/chat/completions") if base_url else "https://api.deepseek.com/chat/completions"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "You output valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
            "stream": False,
        },
        timeout=180,
    )
    usage: dict[str, Any] = {}
    try:
        payload = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError(response.text[:1000])
    if response.status_code >= 400:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False)[:2000])
    usage = payload.get("usage") or {}
    content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return str(content), usage


def parse_valid_artifact(text: str, sample: str) -> tuple[dict[str, Any] | None, str | None]:
    candidates: list[dict[str, Any]] = []
    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, dict):
            candidates.append(parsed)
    except json.JSONDecodeError:
        pass
    candidates.extend(extract_json_objects(text))
    last_error = "no JSON object found"
    for obj in reversed(candidates):
        if obj.get("sample_id") != sample:
            last_error = "sample_id mismatch"
            continue
        if set(obj) != {"sample_id", "fine_trace", "vuln_logic"}:
            last_error = "top-level keys mismatch"
            continue
        obj = repair_artifact(obj, sample=sample)
        raw = json.dumps(obj, ensure_ascii=False)
        error = validate_analysis_artifact_quality(raw)
        if error:
            last_error = error
            continue
        return obj, None
    return None, last_error


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


def _norm_path(value: Any) -> str:
    path = str(value or "").replace("\\", "/").strip()
    for prefix in ("repo-vul/src-vul/", "src-vul/", "./"):
        while path.startswith(prefix):
            path = path[len(prefix):]
    return re.sub(r"/+", "/", path)


def _norm_func(value: Any) -> str:
    text = str(value or "").strip().split("(", 1)[0].strip()
    parts = text.split()
    if parts:
        text = parts[-1]
    return re.sub(r"\s+", "", text)


def _same_anchor(left: dict[str, Any], right: dict[str, Any]) -> bool:
    lf = _norm_path(left.get("file"))
    rf = _norm_path(right.get("file"))
    return (
        bool(lf and rf)
        and (lf == rf or lf.endswith("/" + rf) or rf.endswith("/" + lf))
        and _norm_func(left.get("function")) == _norm_func(right.get("function"))
        and left.get("line") == right.get("line")
        and isinstance(left.get("line"), int)
    )


def _matching_trace_step(artifact: dict[str, Any], loc: dict[str, Any]) -> dict[str, Any] | None:
    trace = artifact.get("fine_trace")
    if not isinstance(trace, list):
        return None
    for step in trace:
        if isinstance(step, dict) and _same_anchor(step, loc):
            return step
    file_name = _norm_path(loc.get("file"))
    function = _norm_func(loc.get("function"))
    for step in trace:
        if not isinstance(step, dict):
            continue
        if (
            _norm_path(step.get("file")) == file_name
            and _norm_func(step.get("function")) == function
            and isinstance(step.get("line"), int)
        ):
            return step
    return None


def _looks_like_source_expression(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if len(text.split()) > 6 and not _SOURCE_EXPR_RE.search(text):
        return False
    if text.lower().startswith(("the ", "a ", "an ")):
        return False
    return bool(_SOURCE_EXPR_RE.search(text) or _IDENT_RE.fullmatch(text))


def _ensure_operands(artifact: dict[str, Any], loc: dict[str, Any], fallback: list[str] | None = None) -> None:
    operands = loc.get("operands")
    if isinstance(operands, list):
        cleaned = [
            str(item).strip()
            for item in operands
            if isinstance(item, str)
            and item.strip()
            and _looks_like_source_expression(item)
        ]
        if cleaned:
            loc["operands"] = cleaned
            return
    step = _matching_trace_step(artifact, loc)
    candidates: list[str] = []
    if step is not None:
        for field in ("var", "code"):
            value = str(step.get(field) or "").strip()
            if value and _looks_like_source_expression(value):
                candidates.append(value)
                break
    if fallback:
        candidates.extend(item for item in fallback if _looks_like_source_expression(item))
    loc["operands"] = candidates[:1] or ["unknown"]


def _expr_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("->", ".").lower()


def _relation_candidates(artifact: dict[str, Any], loc: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for value in loc.get("operands") or []:
        if _looks_like_source_expression(value):
            candidates.append(str(value).strip())
    step = _matching_trace_step(artifact, loc)
    if step is not None:
        for field in ("var", "code"):
            value = str(step.get(field) or "").strip().rstrip(";")
            if value and _looks_like_source_expression(value):
                candidates.append(value)
    unique: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        key = _expr_key(value)
        if key and key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _clean_relation(artifact: dict[str, Any], loc: dict[str, Any]) -> None:
    relation = loc.get("relation")
    if not isinstance(relation, dict):
        relation = {"op": "ne", "left": "", "right": ""}
    op = relation.get("op") if relation.get("op") in _REL_OPS else "ne"
    candidates = _relation_candidates(artifact, loc)
    if not candidates:
        candidates = ["unknown", "0"]

    left = str(relation.get("left") or "").strip()
    if not _looks_like_source_expression(left):
        left = candidates[0]

    right = str(relation.get("right") or "").strip()
    if not _looks_like_source_expression(right):
        right = next((item for item in candidates if _expr_key(item) != _expr_key(left)), "")
        if not right:
            right = "0" if _expr_key(left) != "0" else "1"

    if op in {"eq", "same_object"} and _expr_key(left) == _expr_key(right):
        replacement = next((item for item in candidates if _expr_key(item) != _expr_key(left)), "")
        if replacement:
            right = replacement
        else:
            op = "ne"
            right = "0" if _expr_key(left) != "0" else "1"

    loc["relation"] = {"op": op, "left": left, "right": right}


def _coerce_location(artifact: dict[str, Any], loc: dict[str, Any], *, sample: str | None = None) -> None:
    if "line" in loc:
        loc["line"] = _coerce_line(loc["line"])
    if not isinstance(loc.get("line"), int):
        step = _matching_trace_step(artifact, loc)
        if step is not None and isinstance(step.get("line"), int):
            loc["line"] = step["line"]
        elif sample is not None:
            resolved = _resolve_line_from_source(sample, artifact, loc)
            if resolved is not None:
                loc["line"] = resolved


def _project_roles(artifact: dict[str, Any], *, sample: str | None = None) -> None:
    trace = artifact.get("fine_trace")
    logic = artifact.get("vuln_logic")
    if not isinstance(trace, list) or not isinstance(logic, dict):
        return
    for role in ("source", "root_cause", "sink"):
        point = logic.get(role)
        if not isinstance(point, dict):
            continue
        matching = _matching_trace_step(artifact, point)
        role_steps = [step for step in trace if isinstance(step, dict) and step.get("role") == role]
        if matching is not None and len(role_steps) != 1:
            for step in trace:
                if isinstance(step, dict) and step.get("role") == role:
                    step["role"] = "intermediate"
            matching["role"] = role
        role_steps = [step for step in trace if isinstance(step, dict) and step.get("role") == role]
        if len(role_steps) == 1:
            step = role_steps[0]
            for field in ("file", "function"):
                if field in step:
                    point[field] = step[field]
            if isinstance(step.get("line"), int):
                point["line"] = step["line"]
            elif isinstance(point.get("line"), int):
                step["line"] = point["line"]
            elif sample is not None:
                resolved = _resolve_line_from_source(sample, artifact, point)
                if resolved is not None:
                    point["line"] = resolved
                    step["line"] = resolved
    for role in ("source", "root_cause", "sink"):
        point = logic.get(role)
        if not isinstance(point, dict):
            continue
        role_steps = [step for step in trace if isinstance(step, dict) and step.get("role") == role]
        if role_steps:
            continue
        matching = _matching_trace_step(artifact, point)
        if matching is None:
            continue
        if matching.get("role") in {"source", "root_cause", "sink"}:
            clone = json.loads(json.dumps(matching, ensure_ascii=False))
            clone["role"] = role
            trace.append(clone)
        else:
            matching["role"] = role
    for index, step in enumerate([item for item in trace if isinstance(item, dict)], 1):
        step["step"] = index


def _source_candidates(sample: str, file_value: Any) -> list[Path]:
    if not sample.startswith("arvo_"):
        return []
    arvo_id = sample.split("_", 1)[1]
    source_root = GT_ROOT / "external" / "cybergym_data_subset" / "data" / "arvo" / arvo_id / "repo-vul" / "src-vul"
    wanted = _norm_path(file_value)
    candidates: list[Path] = []
    direct = source_root / wanted
    if direct.is_file():
        candidates.append(direct)
    basename = Path(wanted).name
    if basename:
        try:
            for path in source_root.rglob(basename):
                if path.is_file() and path not in candidates:
                    candidates.append(path)
                    if len(candidates) >= 8:
                        break
        except OSError:
            pass
    stem = Path(wanted).stem
    if stem and len(candidates) < 8:
        try:
            for path in source_root.rglob(stem + ".*"):
                if path.is_file() and path not in candidates:
                    candidates.append(path)
                    if len(candidates) >= 8:
                        break
        except OSError:
            pass
    return candidates


def _all_source_candidates(sample: str) -> list[Path]:
    if not sample.startswith("arvo_"):
        return []
    arvo_id = sample.split("_", 1)[1]
    source_root = GT_ROOT / "external" / "cybergym_data_subset" / "data" / "arvo" / arvo_id / "repo-vul" / "src-vul"
    suffixes = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
    candidates: list[Path] = []
    try:
        for path in source_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in suffixes:
                candidates.append(path)
    except OSError:
        pass
    return candidates


def _function_definition_span(lines: list[str], function_value: Any) -> tuple[int, int] | None:
    function = _norm_func(function_value)
    if not function:
        return None
    names = [function]
    if "::" in function:
        names.append(function.rsplit("::", 1)[-1])
    definition_line: int | None = None
    for name in names:
        definition_re = re.compile(rf"\b{re.escape(name)}\s*\(")
        for index, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith(("*", "/*", "//")):
                continue
            if not definition_re.search(line):
                continue
            if ";" in stripped and "{" not in stripped:
                continue
            definition_line = index
            break
        if definition_line is not None:
            break
    if definition_line is None:
        return None

    open_line: int | None = None
    search_end = min(len(lines), definition_line + 8)
    for index in range(definition_line, search_end + 1):
        if "{" in lines[index - 1]:
            open_line = index
            break
        if ";" in lines[index - 1]:
            break
    if open_line is None:
        return definition_line, definition_line

    depth = 0
    for index in range(open_line, len(lines) + 1):
        line = lines[index - 1]
        depth += line.count("{")
        depth -= line.count("}")
        if depth <= 0 and index > open_line:
            return definition_line, index
    return definition_line, len(lines)


def _match_hint_lines(lines: list[str], hints: list[str], span: tuple[int, int] | None) -> int | None:
    if span is None:
        start, end = 1, len(lines)
    else:
        start, end = span
    scoped_lines = list(enumerate(lines[start - 1 : end], start))
    for hint in hints:
        normalized_hint = re.sub(r"\s+", " ", hint.strip().rstrip(";"))
        if not normalized_hint:
            continue
        matches: list[int] = []
        for index, line in scoped_lines:
            normalized_line = re.sub(r"\s+", " ", line.strip().rstrip(";"))
            if not normalized_line:
                continue
            if normalized_hint in normalized_line or normalized_line in normalized_hint:
                matches.append(index)
        if len(matches) == 1:
            return matches[0]
    for hint in hints:
        if not _looks_like_source_expression(hint):
            continue
        matches = [index for index, line in scoped_lines if hint in line]
        if len(matches) == 1:
            return matches[0]
    return None


def _trace_hint_for_location(artifact: dict[str, Any], loc: dict[str, Any]) -> tuple[str, str]:
    step = _matching_trace_step(artifact, loc)
    if step is None:
        trace = artifact.get("fine_trace")
        if isinstance(trace, list):
            for item in trace:
                if not isinstance(item, dict):
                    continue
                if (
                    _norm_path(item.get("file")) == _norm_path(loc.get("file"))
                    and _norm_func(item.get("function")) == _norm_func(loc.get("function"))
                ):
                    step = item
                    break
    if step is None:
        return "", ""
    return str(step.get("code") or "").strip(), str(step.get("var") or "").strip()


def _resolve_line_from_source(sample: str, artifact: dict[str, Any], loc: dict[str, Any]) -> int | None:
    code_hint, var_hint = _trace_hint_for_location(artifact, loc)
    hints = [hint for hint in (code_hint, var_hint) if hint]
    fallback_definition: int | None = None
    for path in _source_candidates(sample, loc.get("file")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        function_span = _function_definition_span(lines, loc.get("function"))
        resolved = _match_hint_lines(lines, hints, function_span)
        if resolved is not None:
            return resolved
        resolved = _match_hint_lines(lines, hints, None)
        if resolved is not None:
            return resolved
        if function_span is not None and fallback_definition is None:
            fallback_definition = function_span[0]
    if hints:
        for path in _all_source_candidates(sample):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            function_span = _function_definition_span(lines, loc.get("function"))
            resolved = _match_hint_lines(lines, hints, function_span)
            if resolved is not None:
                return resolved
            resolved = _match_hint_lines(lines, hints, None)
            if resolved is not None:
                return resolved
    if fallback_definition is not None:
        return fallback_definition
    return None


def repair_artifact(obj: dict[str, Any], *, sample: str | None = None) -> dict[str, Any]:
    artifact = json.loads(json.dumps(obj, ensure_ascii=False))
    trace = artifact.get("fine_trace")
    if isinstance(trace, list):
        for index, step in enumerate(trace, 1):
            if isinstance(step, dict):
                step["step"] = index
                if "line" in step:
                    step["line"] = _coerce_line(step["line"])
                if sample is not None and not isinstance(step.get("line"), int):
                    resolved = _resolve_line_from_source(sample, artifact, step)
                    if resolved is not None:
                        step["line"] = resolved
    logic = artifact.get("vuln_logic")
    if isinstance(logic, dict):
        allowed = {"source", "root_cause", "sink", "propagation", "issue_alignment"}
        for key in list(logic):
            if key not in allowed:
                logic.pop(key, None)
        logic.setdefault("propagation", [])
        alignment = logic.get("issue_alignment")
        expected_alignment = {"admission", "source", "root_cause", "propagation", "sink"}
        if "issue_alignment" in logic and (
            not isinstance(alignment, dict)
            or set(alignment) != expected_alignment
            or not all(isinstance(alignment.get(field), str) and alignment[field].strip() for field in expected_alignment)
        ):
            logic.pop("issue_alignment", None)
        for label in ("source", "root_cause", "sink"):
            loc = logic.get(label)
            if isinstance(loc, dict):
                _coerce_location(artifact, loc, sample=sample)
                _ensure_operands(artifact, loc)
        _project_roles(artifact, sample=sample)
        for label in ("root_cause", "sink"):
            loc = logic.get(label)
            if isinstance(loc, dict):
                _clean_relation(artifact, loc)
        for edge in logic.get("propagation") or []:
            if not isinstance(edge, dict):
                continue
            via = edge.get("via")
            clean_via = []
            if isinstance(via, list):
                clean_via = [
                    str(item).strip()
                    for item in via
                    if isinstance(item, str)
                    and item.strip()
                    and _looks_like_source_expression(item)
                ]
            endpoint_operands: list[str] = []
            for endpoint in ("from", "to"):
                loc = edge.get(endpoint)
                if isinstance(loc, dict):
                    _coerce_location(artifact, loc, sample=sample)
                    _ensure_operands(artifact, loc, fallback=clean_via)
                    endpoint_operands.extend(loc.get("operands") or [])
            if not clean_via:
                clean_via = [
                    str(item)
                    for item in endpoint_operands
                    if isinstance(item, str) and item.strip() and _looks_like_source_expression(item)
                ]
            edge["via"] = clean_via[:2] or ["order"]
            relation = edge.get("relation")
            if isinstance(relation, dict):
                dummy_loc = {
                    "file": (edge.get("to") or {}).get("file") if isinstance(edge.get("to"), dict) else "",
                    "function": (edge.get("to") or {}).get("function") if isinstance(edge.get("to"), dict) else "",
                    "line": (edge.get("to") or {}).get("line") if isinstance(edge.get("to"), dict) else None,
                    "operands": endpoint_operands,
                    "relation": relation,
                }
                _clean_relation(artifact, dummy_loc)
                edge["relation"] = dummy_loc["relation"]
    return artifact


def finalize_one(args: argparse.Namespace, sample: str) -> dict[str, Any]:
    result_root = RESULTS_ROOT / args.namespace
    sample_dir = result_root / sample
    if sample == "arvo_10999":
        return {"sample": sample, "status": "skipped_environment_rerun"}
    if (sample_dir / "analysis.json").is_file() and not args.force:
        return {"sample": sample, "status": "skipped_existing_analysis"}
    prompt = build_prompt(sample, sample_dir, max_events=args.max_events, max_chars=args.max_chars)
    recovery_dir = sample_dir / "checkpoint" / "analysis_finalization_direct"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    (recovery_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    api_key = load_env_key(args.api_key_env)
    started = time.monotonic()
    content, usage = call_deepseek(
        api_key,
        args.base_url,
        args.model,
        prompt,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
    )
    (recovery_dir / "response.txt").write_text(content, encoding="utf-8")
    artifact, error = parse_valid_artifact(content, sample)
    retry_usage = None
    if artifact is None and args.retry:
        repair_prompt = (
            prompt
            + "\n\nThe previous output was invalid for this reason:\n"
            + str(error)
            + "\n\nPrevious output:\n"
            + content[:12000]
            + "\n\nReturn only a corrected JSON object."
        )
        content2, retry_usage = call_deepseek(
            api_key,
            args.base_url,
            args.model,
            repair_prompt,
            max_tokens=args.max_tokens,
            reasoning_effort=args.reasoning_effort,
        )
        (recovery_dir / "response_retry.txt").write_text(content2, encoding="utf-8")
        artifact, error = parse_valid_artifact(content2, sample)
        content = content2
    elapsed = round(time.monotonic() - started, 1)
    meta = {
        "sample": sample,
        "model": args.model,
        "status": "recovered" if artifact is not None else "failed",
        "seconds": elapsed,
        "usage": usage,
        "retry_usage": retry_usage,
        "validation_error": error,
    }
    (recovery_dir / "metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if artifact is None:
        return meta
    (sample_dir / "analysis.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest_path = sample_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["analysis"] = {
            "produced": True,
            "source": "checkpoint_direct_no_tool_finalization",
            "path": "analysis.json",
            "format": "JSON object with sample_id, fine_trace, and vuln_logic",
        }
        manifest["analysis_recovery"] = meta
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--max-events", type=int, default=80)
    parser.add_argument("--max-chars", type=int, default=60000)
    parser.add_argument("--max-tokens", type=int, default=12000)
    parser.add_argument(
        "--reasoning-effort",
        default="none",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    )
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("samples", nargs="*")
    args = parser.parse_args()
    samples = list(args.samples)
    if args.summary is not None:
        samples.extend(failed_samples_from_summary(args.summary))
    samples = list(dict.fromkeys(samples))
    for sample in samples:
        print(json.dumps(finalize_one(args, sample), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
