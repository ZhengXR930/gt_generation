#!/usr/bin/env python3
"""Validate and compile minimal Reward-Agent probe plans for the GDB runner.

The production path uses ``validate_probe_plan`` and ``apply_probe_plan``.
Legacy mapping helpers below remain only for reading older experiment caches.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

import reward_guidance
from typing import Any


SYSTEM_PROMPT = """You are the runtime-planning role of an external Reward
Agent for vulnerability reproduction. You receive a task Reward Map generated
only from the public issue and vulnerable codebase, plus a coding agent's
ordered fine trace. You
must not add, rewrite, or correct any trace step and you never receive hidden
ground truth. Select only among the supplied step numbers.

Choose admission_step only when a trace step is a parser, validation, or dispatch
gate showing acceptance as the intended input type; harness entry alone is not
enough. Choose root_step only when a trace step directly represents the
issue-stated operation or safety-property violation. Choose consumer_step only
when it occurs after root_step and is a distinct downstream use capable of
turning the violation into a security-visible effect. Use null when the trace
does not support an anchor. Do not infer missing locations.

For each non-null anchor, evidence must be a verbatim contiguous excerpt from
that selected trace step's function, var, code, or note.
Return exactly one JSON object with this shape:
{
  "admission_step": integer | null,
  "admission_evidence": string | null,
  "root_step": integer | null,
  "root_evidence": string | null,
  "consumer_step": integer | null,
  "consumer_evidence": string | null,
  "runtime_observations": [
    {"step": integer, "name": "short_identifier", "expression": "verbatim trace expression"}
  ],
  "reason": "brief explanation"
}

Select at most four runtime observations. They are diagnostic probes, not
claims and not repair advice. Prefer scalar values, lengths, capacities,
indices, state flags, and pointers that distinguish Admission from the Root
condition. Every expression must be a verbatim contiguous substring of the
selected step's function, var, code, or note and plausibly evaluable by GDB at
that step. Do not use function calls, string literals, assignments, or invent
an expression absent from the trace. Use an empty list when no safe expression
is present. Runtime observations can never by themselves rewrite the issue or
make the untrusted trace true.
"""

_OBSERVATION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,47}$")
_PROBE_STAGES = ("admission", "root", "propagation")
_CODE_MARKERS = re.compile(r"(?:[;{}()[\]=]|->|::|\breturn\b|\bif\b|\bwhile\b)")
_SOURCE_PREFIXES = (
    "repo-vul/", "src-vul/", "src/", "source/",
)


def _safe_observation_expression(expression: str) -> bool:
    """Accept passive scalar/pointer expressions, never executable calls."""
    if not expression or len(expression) > 160:
        return False
    if any(token in expression for token in (";", "\n", "\r", "`", '"', "'", "=")):
        return False
    if not re.search(r"[A-Za-z_][A-Za-z0-9_]*", expression):
        return False
    # Parenthesized casts are allowed, but identifier(...) calls are not.
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(", expression):
        return False
    return True


def _normalize_observation_expression(expression: Any) -> str | None:
    """Reduce an observed assignment statement to its passive RHS value."""
    if not isinstance(expression, str):
        return None
    candidates = [expression.strip().rstrip(";, ")]
    assignment = re.search(r"(?<![=!<>])=(?!=)", expression)
    if assignment:
        candidates.insert(0, expression[assignment.end():].strip().rstrip(";, "))
    for candidate in candidates:
        if _safe_observation_expression(candidate):
            return candidate
    return None


def call_mapper(
    skeleton: dict[str, Any],
    trace: list[dict[str, Any]],
    api_key: str,
    *,
    model: str = "deepseek-chat",
    api_url: str = "https://api.deepseek.com/chat/completions",
) -> dict[str, Any]:
    public_skeleton = {
        "claims": skeleton.get("claims", {}),
        "root_hypothesis": skeleton.get("root_hypothesis", {}),
        "unknowns": skeleton.get("unknowns", []),
        "stages": skeleton.get("stages", {}),
    }
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"issue_skeleton": public_skeleton, "fine_trace": trace},
                        ensure_ascii=False,
                    ),
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
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.loads(response.read().decode())
    return json.loads(result["choices"][0]["message"]["content"])


def _step_text(step: dict[str, Any]) -> str:
    return "\n".join(str(step.get(key) or "") for key in ("function", "var", "code", "note"))


def _compact_code(value: str) -> str:
    """Normalize source snippets for a whitespace-insensitive exact check."""
    value = re.sub(r"//.*", "", value)
    value = re.sub(r"/\*.*?\*/", "", value)
    return re.sub(r"\s+", "", value).rstrip(";")


def _source_candidates(codebase: Path, declared_file: str) -> list[Path]:
    normalized = declared_file.replace("\\", "/").lstrip("./")
    relatives = [normalized]
    for prefix in _SOURCE_PREFIXES:
        if normalized.startswith(prefix):
            relatives.append(normalized[len(prefix):])
    candidates: list[Path] = []
    for relative in relatives:
        path = (codebase / relative).resolve()
        if path.is_file() and (path == codebase or codebase.resolve() in path.parents):
            candidates.append(path)
    if candidates:
        return list(dict.fromkeys(candidates))
    parts = [part for part in normalized.split("/") if part]
    for path in codebase.rglob(parts[-1] if parts else "__missing__"):
        if not path.is_file():
            continue
        path_parts = path.relative_to(codebase).parts
        common = 0
        for left, right in zip(reversed(parts), reversed(path_parts)):
            if left != right:
                break
            common += 1
        if common >= min(2, len(parts)):
            candidates.append(path.resolve())
    return list(dict.fromkeys(candidates))


def _matching_code_lines(lines: list[str], claimed_code: str) -> list[int]:
    """Find bounded public-source locations supporting a trace code claim."""
    snippets = [part.strip() for part in claimed_code.split(";") if part.strip()]
    matches: set[int] = set()
    for snippet in snippets[:4]:
        compact = _compact_code(snippet)
        if len(compact) < 6:
            continue
        for number, line in enumerate(lines, 1):
            source = _compact_code(line)
            if compact in source or (len(source) >= 6 and source in compact):
                matches.add(number)
                if len(matches) >= 8:
                    return sorted(matches)
    return sorted(matches)


def _repair_code_range(
    lines: list[str], claimed_code: str, function: str,
) -> tuple[int, int] | None:
    """Relocate a source-like claim inside its declared function.

    Multiple statements may live on separate nearby source lines.  We accept a
    repair only when the best monotonic, bounded match is unique; ambiguity is
    left unresolved instead of silently choosing a convenient breakpoint.
    """
    span = _function_span(lines, function)
    if span is None:
        return None
    start, end = span
    snippets = [
        _compact_code(part) for part in claimed_code.split(";")
        # Short parser statements such as ``s >> c`` are meaningful when they
        # are unique inside the declared function.
        if len(_compact_code(part)) >= 3
    ][:4]
    if not snippets:
        return None
    candidates: list[list[int]] = []
    for snippet in snippets:
        locations = []
        for number in range(start, end + 1):
            source = _compact_code(lines[number - 1])
            if snippet in source or (len(source) >= 6 and source in snippet):
                locations.append(number)
        if not locations:
            return None
        candidates.append(locations[:16])

    ranges: set[tuple[int, int]] = set()

    def visit(index: int, chosen: list[int]) -> None:
        if len(ranges) > 32:
            return
        if index == len(candidates):
            if chosen[-1] - chosen[0] <= 12:
                ranges.add((chosen[0], chosen[-1]))
            return
        for number in candidates[index]:
            if chosen and (number < chosen[-1] or number - chosen[-1] > 12):
                continue
            visit(index + 1, [*chosen, number])

    visit(0, [])
    if not ranges:
        return None
    shortest = min(right - left for left, right in ranges)
    best = sorted(item for item in ranges if item[1] - item[0] == shortest)
    return best[0] if len(best) == 1 else None


def validate_trace_source_anchors(
    trace: list[dict[str, Any]], codebase: Path
) -> list[dict[str, Any]]:
    """Validate candidate line/code pairs against the public vulnerable tree.

    A prose description is retained as an unverified semantic claim.  Only a
    source-backed code snippet may earn exact-line evidence later in GDB.
    """
    root = codebase.resolve()
    enriched: list[dict[str, Any]] = []
    for step in trace:
        item = dict(step)
        declared_file = str(step.get("file") or "").strip()
        line = step.get("line")
        line_end = step.get("line_end")
        code = str(step.get("code") or "").strip()
        result: dict[str, Any] = {
            "status": "unverifiable",
            "declared_file": declared_file,
            "declared_line": line if isinstance(line, int) else None,
            "reason": "line_or_file_missing",
        }
        candidates = _source_candidates(root, declared_file) if declared_file else []
        if declared_file and not candidates:
            result.update(status="invalid", reason="source_file_not_found")
        elif len(candidates) > 1:
            result.update(
                status="invalid",
                reason="source_file_ambiguous",
                candidate_files=[str(path.relative_to(root)) for path in candidates[:8]],
            )
        elif candidates:
            source = candidates[0]
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
            result["resolved_file"] = str(source.relative_to(root))
            code_like = bool(_CODE_MARKERS.search(code))
            if not isinstance(line, int) or line < 1 or line > len(lines):
                repaired = (
                    _repair_code_range(lines, code, str(step.get("function") or ""))
                    if code_like else None
                )
                if repaired is None:
                    result.update(status="invalid", reason="declared_line_out_of_range")
                else:
                    repaired_start, repaired_end = repaired
                    result.update(
                        status="repaired",
                        reason="out_of_range_line_relocated_within_function",
                        resolved_line=repaired_start,
                        resolved_line_end=repaired_end,
                        resolved_source_text="\n".join(
                            lines[repaired_start - 1:repaired_end]
                        )[:800],
                    )
                    item["line"] = repaired_start
                    if repaired_end > repaired_start:
                        item["line_end"] = repaired_end
                    else:
                        item.pop("line_end", None)
            else:
                end = line_end if isinstance(line_end, int) and line_end >= line else line
                end = min(end, len(lines), line + 8)
                source_text = "\n".join(lines[line - 1:end])
                result["source_text"] = source_text[:800]
                compact_claim = _compact_code(code)
                compact_source = _compact_code(source_text)
                matches = _matching_code_lines(lines, code) if code_like else []
                if (
                    code_like
                    and len(compact_claim) >= 3
                    and compact_claim in compact_source
                ):
                    result.update(status="valid", reason="line_code_match")
                elif code_like:
                    repaired = _repair_code_range(
                        lines, code, str(step.get("function") or "")
                    )
                    if repaired is not None:
                        repaired_start, repaired_end = repaired
                        result.update(
                            status="repaired",
                            reason="line_code_relocated_within_function",
                            matching_lines=matches,
                            resolved_line=repaired_start,
                            resolved_line_end=repaired_end,
                            resolved_source_text="\n".join(
                                lines[repaired_start - 1:repaired_end]
                            )[:800],
                        )
                        item["line"] = repaired_start
                        if repaired_end > repaired_start:
                            item["line_end"] = repaired_end
                        else:
                            item.pop("line_end", None)
                    else:
                        result.update(
                            status="invalid",
                            reason="line_code_mismatch_unresolved",
                            matching_lines=matches,
                        )
                else:
                    result.update(
                        status="unverifiable",
                        reason="code_is_description_not_source_statement",
                    )
        item["anchor_validation"] = result
        enriched.append(item)
    return enriched


def _function_span(lines: list[str], function: str) -> tuple[int, int] | None:
    short = function.split("::")[-1].split("(")[0].strip()
    if not short:
        return None
    for index, line in enumerate(lines):
        if not re.search(rf"\b{re.escape(short)}\s*\(", line):
            continue
        start = index
        brace = 0
        opened = False
        for cursor in range(index, min(len(lines), index + 1200)):
            text = re.sub(r'"(?:\\.|[^"\\])*"', '""', lines[cursor])
            brace += text.count("{") - text.count("}")
            opened = opened or "{" in text
            if opened and brace <= 0:
                return start + 1, cursor + 1
    return None


def _split_call_arguments(text: str, call: str) -> list[str]:
    marker = text.find(call)
    if marker < 0:
        return []
    start = text.find("(", marker + len(call))
    if start < 0:
        return []
    depth = 0
    current: list[str] = []
    result: list[str] = []
    for char in text[start + 1:]:
        if char == "(" or char == "[":
            depth += 1
        elif char == ")" or char == "]":
            if char == ")" and depth == 0:
                result.append("".join(current).strip())
                return result
            depth -= 1
        if char == "," and depth == 0:
            result.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    return []


def derive_read_raw_data_checkpoints(
    *, codebase: Path, reward_spec: dict[str, Any] | None,
    trace: list[dict[str, Any]], limit: int = 16,
) -> list[dict[str, Any]]:
    """Derive bounded call/return probes solely from public source anchors."""
    root = codebase.resolve()
    anchors: set[tuple[str, str]] = set(reward_guidance.stage_anchors(reward_spec))
    for step in trace:
        validation = step.get("anchor_validation") or {}
        resolved = str(validation.get("resolved_file") or "")
        function = str(step.get("function") or "")
        if resolved and function:
            anchors.add((resolved, function))

    checkpoints: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for declared_file, function in sorted(anchors):
        candidates = _source_candidates(root, declared_file)
        if len(candidates) != 1:
            continue
        source = candidates[0]
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        span = _function_span(lines, function)
        if span is None:
            continue
        start, end = span
        for number in range(start, end + 1):
            source_line = lines[number - 1]
            if "readRawData" not in source_line:
                continue
            key = (str(source.relative_to(root)), number)
            if key in seen:
                continue
            args = _split_call_arguments(source_line, "readRawData")
            if len(args) < 2:
                continue
            requested = args[1].strip()
            if not _safe_observation_expression(requested):
                continue
            # Break at the callee rather than the caller's source line. In an
            # optimized build the caller expression may already be gone, while
            # QDataStream::readRawData's length parameter and return value have
            # stable ABI/debug identities at callee entry/return.
            captures: dict[str, str] = {"requested_bytes": "(int)$edx"}
            # Optimized caller locals are not reliable GDB evidence. Branch
            # facts are instead proven by exclusive source callsites/body
            # checkpoints below; keep this field for the generic runner API.
            caller_captures: dict[str, str] = {}
            seen.add(key)
            source_relative = str(source.relative_to(root)).replace("\\", "/")
            debug_file = (
                source_relative[len("src-vul/"):]
                if source_relative.startswith("src-vul/")
                else source_relative
            )
            static_branch_facts: dict[str, Any] = {}
            # The hit callsite itself proves which side of this public-source
            # branch executed, even when optimized locals are unavailable.
            if re.search(r"readRawData\s*\(\s*pixel\s*,", source_line):
                static_branch_facts["packet_is_rle"] = True
            elif re.search(r"readRawData\s*\(\s*dst\s*,", source_line):
                static_branch_facts["packet_is_rle"] = False
            checkpoints.append({
                "kind": "call_observation",
                "event_point": f"runtime_call:readRawData:{len(checkpoints) + 1}",
                "assertion_role": ["runtime_fact"],
                "expected_order": len(checkpoints),
                "file": debug_file,
                "function": function,
                "line": number,
                "code": source_line.strip(),
                "captures": captures,
                "caller_captures": caller_captures,
                "source_requested_expression": requested,
                "call_name": "readRawData",
                "break_function": "QDataStream::readRawData",
                "requested_capture": "requested_bytes",
                "return_capture": "returned_bytes",
                "branch_captures": [
                    name for name in ("packet_header", "packet_is_rle", "palette_mode", "grey_mode")
                    if name in caller_captures
                ],
                "static_branch_facts": static_branch_facts,
                "allow_function_fallback": True,
                "max_hits_per_breakpoint": 32,
            })
            if len(checkpoints) >= limit:
                return checkpoints
    return checkpoints


def derive_key_branch_checkpoints(
    *, codebase: Path, reward_spec: dict[str, Any] | None,
    trace: list[dict[str, Any]], limit: int = 12,
) -> list[dict[str, Any]]:
    """Derive branch facts from executed public-source branch bodies.

    This is deliberately narrower than arbitrary expression capture: a hit in
    an exclusive branch body proves the controlling fact even when optimized
    locals such as ``info`` are unavailable to GDB.
    """
    root = codebase.resolve()
    anchors: set[tuple[str, str]] = set(reward_guidance.stage_anchors(reward_spec))
    for step in trace:
        validation = step.get("anchor_validation") or {}
        resolved = str(validation.get("resolved_file") or "")
        function = str(step.get("function") or "")
        if resolved and function:
            anchors.add((resolved, function))

    checkpoints: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for declared_file, function in sorted(anchors):
        candidates = _source_candidates(root, declared_file)
        if len(candidates) != 1:
            continue
        source = candidates[0]
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        span = _function_span(lines, function)
        if span is None:
            continue
        start, end = span
        pal_condition = next(
            (n for n in range(start, end + 1) if re.search(r"\bif\s*\(\s*info\.pal\s*\)", lines[n - 1])),
            None,
        )
        grey_condition = next(
            (n for n in range(start, end + 1) if re.search(r"\belse\s+if\s*\(\s*info\.grey\s*\)", lines[n - 1])),
            None,
        )
        if pal_condition is None or grey_condition is None or grey_condition <= pal_condition:
            continue

        # Choose statements that can execute only inside the corresponding
        # branch. The patterns describe observable operations, not issue/GT
        # semantics, and are bounded to this source function.
        pal_line = next(
            (n for n in range(pal_condition + 1, grey_condition) if re.search(r"\b(?:idx|palette)\b", lines[n - 1]) and ";" in lines[n - 1]),
            None,
        )
        grey_line = next(
            (n for n in range(grey_condition + 1, end + 1) if "qRgb(" in lines[n - 1]),
            None,
        )
        true_line = next(
            (n for n in range((grey_line or grey_condition) + 1, end + 1) if re.search(r"\btga\.pixel_size\b", lines[n - 1])),
            None,
        )
        branch_lines = (
            (pal_line, {"palette_mode": True, "grey_mode": False}),
            (grey_line, {"palette_mode": False, "grey_mode": True}),
            (true_line, {"palette_mode": False, "grey_mode": False}),
        )
        source_relative = str(source.relative_to(root)).replace("\\", "/")
        debug_file = source_relative[len("src-vul/"):] if source_relative.startswith("src-vul/") else source_relative
        for number, facts in branch_lines:
            if number is None or (debug_file, number) in seen:
                continue
            seen.add((debug_file, number))
            checkpoints.append({
                "kind": "branch_observation",
                "event_point": f"runtime_branch:input_mode:{len(checkpoints) + 1}",
                "assertion_role": ["runtime_fact"],
                "expected_order": len(checkpoints),
                "file": debug_file,
                "function": function,
                "line": number,
                "code": lines[number - 1].strip(),
                "captures": {},
                "static_branch_facts": facts,
                "allow_function_fallback": False,
                "max_hits_per_breakpoint": 1,
            })
            if len(checkpoints) >= limit:
                return checkpoints
    return checkpoints


_CALL_CONTROL_WORDS = {"if", "for", "while", "switch", "return", "sizeof", "assert"}
_AMD64_ARGUMENT_REGISTERS = ("$rdi", "$rsi", "$rdx", "$rcx", "$r8", "$r9")


def _step_source(
    root: Path, step: dict[str, Any]
) -> tuple[Path, list[str], int, int] | None:
    validation = step.get("anchor_validation") or {}
    relative = str(validation.get("resolved_file") or step.get("file") or "")
    candidates = _source_candidates(root, relative)
    if len(candidates) != 1 or not isinstance(step.get("line"), int):
        return None
    source = candidates[0]
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    start = int(step["line"])
    end = int(step.get("line_end") or start)
    if not 1 <= start <= end <= len(lines):
        return None
    return source, lines, start, end


def _calls_in_range(lines: list[str], start: int, end: int) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(?:(?P<receiver>[A-Za-z_][A-Za-z0-9_]*)\s*(?:\.|->)\s*)?"
        r"(?P<callee>[A-Za-z_][A-Za-z0-9_:]*)\s*\("
    )
    for number in range(start, end + 1):
        text = lines[number - 1]
        for match in pattern.finditer(text):
            callee = match.group("callee").split("::")[-1]
            if callee in _CALL_CONTROL_WORDS:
                continue
            args = _split_call_arguments(text[match.start():], match.group("callee"))
            if args:
                calls.append({
                    "line": number,
                    "receiver": match.group("receiver"),
                    "callee": match.group("callee"),
                    "arguments": args,
                    "code": text.strip(),
                })
    return calls


def _receiver_type(
    lines: list[str], function: str, receiver: str | None
) -> str | None:
    if not receiver:
        return None
    span = _function_span(lines, function)
    if span is None:
        return None
    start, end = span
    declaration_text = " ".join(lines[max(0, start - 3):min(end, start + 12)])
    matches = re.findall(
        rf"\b([A-Za-z_][A-Za-z0-9_:<>]*)\s+(?:[*&]+\s*)?{re.escape(receiver)}\b",
        declaration_text,
    )
    return matches[-1] if matches else None


def _first_executable_witness(
    lines: list[str], start: int, end: int
) -> int | None:
    for number in range(start, end + 1):
        text = re.sub(r"//.*", "", lines[number - 1]).strip()
        if not text or text in {"{", "}"} or text.startswith("else"):
            continue
        if re.match(r"^(?:assert|Q_ASSERT)\s*\(", text):
            continue
        if re.search(r"(?:\.|->)\s*[A-Za-z_]\w*\s*\(|\b[A-Za-z_]\w*\s*\(", text):
            return number
        if re.search(r"(?:=|\+\+|--|\breturn\b)", text):
            return number
    return None


def _braced_block(lines: list[str], condition_line: int) -> tuple[int, int, int | None] | None:
    """Return true-body range and an optional `else` line."""
    depth = 0
    opened = False
    for number in range(condition_line, min(len(lines), condition_line + 300) + 1):
        text = re.sub(r'"(?:\\.|[^"\\])*"', '""', lines[number - 1])
        for char in text:
            if char == "{":
                depth += 1
                opened = True
            elif char == "}" and opened:
                depth -= 1
                if depth == 0:
                    suffix = text[text.find("}") + 1:]
                    if re.search(r"\belse\b", suffix):
                        return condition_line + 1, number - 1, number
                    next_line = number + 1 if number < len(lines) and re.match(
                        r"\s*else\b", lines[number]
                    ) else None
                    return condition_line + 1, number - 1, next_line
    return None


def _else_body(lines: list[str], else_line: int | None) -> tuple[int, int] | None:
    if else_line is None:
        return None
    text = lines[else_line - 1]
    # `else if` is itself the false-path witness for the preceding predicate.
    if re.search(r"\belse\s+if\b", text):
        return else_line, else_line
    return_range = _braced_block(lines, else_line)
    return (return_range[0], return_range[1]) if return_range else None


def compile_runtime_observation_specs(
    *, codebase: Path, trace: list[dict[str, Any]], plan: dict[str, Any],
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Compile model-level call/branch semantics into verifier-owned probes."""
    root = codebase.resolve()
    checkpoints: list[dict[str, Any]] = []
    call_observations = list(plan.get("call_observations") or [])
    declared_call_steps = {
        observation.get("step") for observation in call_observations
        if isinstance(observation, dict)
    }
    # A Root probe already states that this source step matters to the frozen
    # vulnerable-state contract.  If that repaired step is uniquely a call,
    # lower it to call/return evidence automatically instead of requiring the
    # observation model to repeat the same intent in a second output field.
    for probe in plan.get("probes") or []:
        if (
            isinstance(probe, dict)
            and probe.get("stage") == "root"
            and probe.get("step") not in declared_call_steps
        ):
            call_observations.append({"stage": "root", "step": probe["step"]})
            declared_call_steps.add(probe["step"])
    for observation in call_observations:
        step = trace[observation["step"] - 1]
        context = _step_source(root, step)
        if context is None:
            continue
        source, lines, start, end = context
        calls = _calls_in_range(lines, start, end)
        if len(calls) != 1:
            continue
        call = calls[0]
        receiver_type = _receiver_type(lines, str(step.get("function") or ""), call["receiver"])
        qualified_callee = (
            f"{receiver_type}::{call['callee']}" if receiver_type else call["callee"]
        )
        implicit = 1 if call["receiver"] else 0
        captures: dict[str, str] = {}
        argument_metadata: list[dict[str, Any]] = []
        used_names = {"return_value"}
        for argument_index, source_expression in enumerate(call["arguments"][:5]):
            register_index = argument_index + implicit
            if register_index >= len(_AMD64_ARGUMENT_REGISTERS):
                continue
            identifiers = re.findall(r"[A-Za-z_]\w*", source_expression)
            base_name = identifiers[-1] if identifiers else f"argument_{argument_index}"
            base_name = re.sub(r"\W+", "_", base_name).strip("_") or f"argument_{argument_index}"
            name = base_name
            suffix = 2
            while name in used_names:
                name = f"{base_name}_{suffix}"
                suffix += 1
            used_names.add(name)
            captures[name] = f"(long long){_AMD64_ARGUMENT_REGISTERS[register_index]}"
            argument_metadata.append({
                "index": argument_index,
                "name": name,
                "source_expression": source_expression,
            })
        if not captures:
            continue
        relative = str(source.relative_to(root)).replace("\\", "/")
        debug_file = relative[len("src-vul/"):] if relative.startswith("src-vul/") else relative
        return_name = "return_value"
        checkpoints.append({
            "kind": "call_observation",
            "event_point": f"runtime_call:{len(checkpoints) + 1}",
            "assertion_role": [observation["stage"]],
            "expected_order": len(checkpoints),
            "file": debug_file,
            "function": step.get("function"),
            "line": call["line"],
            "code": call["code"],
            "captures": captures,
            "caller_captures": {},
            "call_name": qualified_callee,
            "break_function": qualified_callee,
            "argument_metadata": argument_metadata,
            "return_capture": return_name,
            "derived_relations": [
                {"name": f"{return_name}_lt_{arg['name']}", "op": "lt", "left": return_name, "right": arg["name"]}
                for arg in argument_metadata
                if re.search(
                    r"(?:len|length|size|count|bytes|capacity)",
                    f"{arg['name']} {arg['source_expression']}",
                    re.IGNORECASE,
                )
            ],
            "allow_function_fallback": True,
            "max_hits_per_breakpoint": 32,
        })
        if len(checkpoints) >= limit:
            return checkpoints

    for observation in plan.get("branch_observations") or []:
        step = trace[observation["step"] - 1]
        context = _step_source(root, step)
        if context is None:
            continue
        source, lines, _, _ = context
        span = _function_span(lines, str(step.get("function") or ""))
        if span is None:
            continue
        predicate = observation["predicate"]
        compact_predicate = _compact_code(predicate)
        condition_lines = [
            number for number in range(span[0], span[1] + 1)
            if re.search(r"\bif\s*\(", lines[number - 1])
            and compact_predicate in _compact_code(lines[number - 1])
        ]
        if len(condition_lines) != 1:
            continue
        block = _braced_block(lines, condition_lines[0])
        if block is None:
            continue
        true_start, true_end, else_line = block
        false_range = _else_body(lines, else_line)
        witnesses = [(True, _first_executable_witness(lines, true_start, true_end))]
        if false_range:
            witnesses.append((False, _first_executable_witness(lines, *false_range)))
        relative = str(source.relative_to(root)).replace("\\", "/")
        debug_file = relative[len("src-vul/"):] if relative.startswith("src-vul/") else relative
        for outcome, number in witnesses:
            if number is None:
                continue
            checkpoints.append({
                "kind": "branch_observation",
                "event_point": f"runtime_branch:{len(checkpoints) + 1}",
                "assertion_role": [observation["stage"]],
                "expected_order": len(checkpoints),
                "file": debug_file,
                "function": step.get("function"),
                "line": number,
                "code": lines[number - 1].strip(),
                "captures": {},
                "branch_predicate": predicate,
                "branch_outcome": outcome,
                "allow_function_fallback": False,
                "max_hits_per_breakpoint": 1,
            })
            if len(checkpoints) >= limit:
                return checkpoints
    return checkpoints


def validate_probe_plan(value: Any, trace: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate the deliberately small Reward-Agent/GDB boundary."""
    expected_keys = {"probes", "call_observations", "branch_observations"}
    if (
        not isinstance(value, dict)
        or "probes" not in value
        or not set(value).issubset(expected_keys)
    ):
        raise ValueError("observation plan requires probes, call_observations, and branch_observations")
    probes = value.get("probes")
    if not isinstance(probes, list):
        raise ValueError("probe plan must contain a probes list")
    result: list[dict[str, Any]] = []
    trace_text = "\n".join(_step_text(step) for step in trace)
    stage_counts = {stage: 0 for stage in _PROBE_STAGES}
    seen: set[tuple[str, int]] = set()
    for probe in probes:
        # This is an LLM-output boundary.  Keep the useful bounded subset of a
        # noisy plan instead of discarding every stage because one extra probe
        # or duplicate was emitted.
        if len(result) >= 6:
            break
        if not isinstance(probe, dict) or set(probe) != {"stage", "step", "captures"}:
            continue
        stage = probe.get("stage")
        step = probe.get("step")
        captures = probe.get("captures")
        if stage not in _PROBE_STAGES:
            continue
        if not isinstance(step, int) or not 1 <= step <= len(trace):
            continue
        if not isinstance(captures, list):
            captures = []
        if (stage, step) in seen:
            continue
        if stage_counts[stage] >= 2:
            continue
        stage_counts[stage] += 1
        seen.add((stage, step))
        normalized: list[dict[str, str]] = []
        names: set[str] = set()
        for capture in captures[:3]:
            # A noisy capture must not discard a useful location probe or the
            # other stages. GDB location reachability remains valid even when
            # one proposed expression cannot be admitted safely.
            if not isinstance(capture, dict) or set(capture) != {"name", "expression"}:
                continue
            name = capture.get("name")
            expression = _normalize_observation_expression(capture.get("expression"))
            if not isinstance(name, str) or not _OBSERVATION_NAME.fullmatch(name):
                continue
            if name in names:
                continue
            if expression is None:
                continue
            if " ".join(expression.split()).lower() not in " ".join(
                trace_text.split()
            ).lower():
                continue
            names.add(name)
            normalized.append({"name": name, "expression": expression})
        result.append({"stage": stage, "step": step, "captures": normalized})
    calls = value.get("call_observations", [])
    if not isinstance(calls, list):
        calls = []
    normalized_calls: list[dict[str, Any]] = []
    for observation in calls:
        if not isinstance(observation, dict) or set(observation) != {"stage", "step"}:
            continue
        stage = observation.get("stage")
        step = observation.get("step")
        if stage not in _PROBE_STAGES or not isinstance(step, int) or not 1 <= step <= len(trace):
            continue
        normalized_calls.append({"stage": stage, "step": step})
        if len(normalized_calls) >= 4:
            break

    branches = value.get("branch_observations", [])
    if not isinstance(branches, list):
        branches = []
    normalized_branches: list[dict[str, Any]] = []
    for observation in branches:
        if not isinstance(observation, dict) or set(observation) != {"stage", "step", "predicate"}:
            continue
        stage = observation.get("stage")
        step = observation.get("step")
        predicate = str(observation.get("predicate") or "").strip()
        if stage not in _PROBE_STAGES or not isinstance(step, int) or not 1 <= step <= len(trace):
            continue
        if not _safe_observation_expression(predicate) or len(predicate) > 120:
            continue
        # Reject prose-shaped outcomes while allowing source predicates such as
        # `c & 0x80`, `length > capacity`, and `info.pal`.  The trace is an
        # untrusted hypothesis and need not quote the source expression
        # verbatim.  The source compiler below is the authority: an invented or
        # ambiguous predicate simply cannot compile into a runtime observation.
        boolean_shape = bool(
            re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", predicate)
            or re.search(r"&&|\|\||==|!=|<=|>=|[&|<>!]", predicate)
        )
        if not boolean_shape:
            continue
        normalized_branches.append({"stage": stage, "step": step, "predicate": predicate})
        if len(normalized_branches) >= 4:
            break
    return {
        "probes": result,
        "call_observations": normalized_calls,
        "branch_observations": normalized_branches,
    }


def apply_probe_plan(
    trace: list[dict[str, Any]], plan: dict[str, Any], root_predicate: str | None
) -> list[dict[str, Any]]:
    """Compile a minimal probe plan into the existing deterministic runner."""
    enriched = [dict(step) for step in trace]
    if enriched:
        enriched[0].setdefault("role", "source")
    for probe in plan.get("probes") or []:
        item = enriched[probe["step"] - 1]
        stage = probe["stage"]
        if stage == "admission":
            item["phase"] = "admission"
        elif stage == "root":
            item["role"] = "root"
            if root_predicate:
                item["invariant"] = root_predicate
        else:
            # A co-located Root/Propagation point may be observed, but cannot
            # establish an ordered downstream propagation transition.
            if str(item.get("role") or "").lower() != "root":
                item["role"] = "sink"
            item["downstream_consumer"] = True
        captures = item.setdefault("captures", {})
        capture_kinds = item.setdefault("observer_capture_kinds", {})
        observer_names = item.setdefault("observer_capture_names", [])
        for capture in probe["captures"]:
            name = f"observer_{stage}_{capture['name']}"
            expression = capture["expression"]
            captures.setdefault(name, expression)
            if expression.startswith("*"):
                capture_kinds.setdefault(name, "dereferenced_value")
            elif re.search(r"\([^)]*\*[^)]*\)", expression):
                capture_kinds.setdefault(name, "address")
            else:
                capture_kinds.setdefault(name, "scalar_or_pointer")
            if name not in observer_names:
                observer_names.append(name)
    return enriched


def validate_mapping(value: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("admission", "root", "consumer"):
        step = value.get(f"{name}_step")
        evidence = value.get(f"{name}_evidence")
        if step is not None and (not isinstance(step, int) or not 1 <= step <= len(trace)):
            raise ValueError(f"invalid {name}_step")
        if step is None:
            if evidence is not None:
                raise ValueError(f"{name}_evidence requires an anchor")
        else:
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError(f"{name}_step requires evidence")
            if " ".join(evidence.lower().split()) not in " ".join(
                _step_text(trace[step - 1]).lower().split()
            ):
                raise ValueError(f"{name}_evidence is not from the selected step")
        result[f"{name}_step"] = step
        result[f"{name}_evidence"] = evidence.strip() if isinstance(evidence, str) else None
    if result["consumer_step"] is not None and (
        result["root_step"] is None or result["consumer_step"] <= result["root_step"]
    ):
        # Consumer evidence is optional diagnostic context. A noisy mapper must
        # not invalidate otherwise usable admission/root runtime verification.
        result["consumer_step"] = None
        result["consumer_evidence"] = None
    observations = value.get("runtime_observations", [])
    if not isinstance(observations, list):
        raise ValueError("runtime_observations must be an array")
    normalized_observations: list[dict[str, Any]] = []
    observation_rejections: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for observation in observations[:4]:
        if not isinstance(observation, dict):
            observation_rejections.append(
                {"observation": observation, "reason": "not an object"}
            )
            continue
        if set(observation) != {"step", "name", "expression"}:
            observation_rejections.append(
                {"observation": observation, "reason": "unexpected fields"}
            )
            continue
        step = observation.get("step")
        name = observation.get("name")
        expression = observation.get("expression")
        if not isinstance(step, int) or not 1 <= step <= len(trace):
            observation_rejections.append(
                {"observation": observation, "reason": "invalid step"}
            )
            continue
        if not isinstance(name, str) or not _OBSERVATION_NAME.fullmatch(name):
            observation_rejections.append(
                {"observation": observation, "reason": "invalid name"}
            )
            continue
        normalized_expression = _normalize_observation_expression(expression)
        if normalized_expression is None:
            observation_rejections.append(
                {"observation": observation, "reason": "unsafe expression"}
            )
            continue
        expression = normalized_expression
        if " ".join(expression.split()).lower() not in " ".join(
            _step_text(trace[step - 1]).split()
        ).lower():
            observation_rejections.append(
                {"observation": observation, "reason": "expression not grounded in step"}
            )
            continue
        key = (step, expression)
        if key in seen:
            continue
        seen.add(key)
        normalized_observations.append(
            {"step": step, "name": name, "expression": expression}
        )
    result["runtime_observations"] = normalized_observations
    result["runtime_observation_rejections"] = observation_rejections
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("mapping reason must be non-empty")
    result["reason"] = reason.strip()
    return result


def apply_mapping(
    trace: list[dict[str, Any]], mapping: dict[str, Any], root_predicate: str | None
) -> list[dict[str, Any]]:
    """Return an instrumentable copy; never alter locations or trace order."""
    enriched = [dict(step) for step in trace]
    admission_step = mapping.get("admission_step")
    root_step = mapping.get("root_step")
    consumer_step = mapping.get("consumer_step")
    if admission_step:
        enriched[admission_step - 1]["phase"] = "admission"
    if enriched:
        enriched[0].setdefault("role", "source")
    if root_step:
        enriched[root_step - 1]["role"] = "root"
        if root_predicate:
            enriched[root_step - 1]["invariant"] = root_predicate
    if consumer_step:
        enriched[consumer_step - 1]["role"] = "sink"
        enriched[consumer_step - 1]["downstream_consumer"] = True
    for observation in mapping.get("runtime_observations") or []:
        step = observation["step"]
        name = "observer_" + observation["name"]
        captures = enriched[step - 1].setdefault("captures", {})
        if isinstance(captures, dict):
            capture_kinds = enriched[step - 1].setdefault(
                "observer_capture_kinds", {}
            )
            captures.setdefault(name, observation["expression"])
            expression = observation["expression"].strip()
            if expression.startswith("*"):
                capture_kinds.setdefault(name, "dereferenced_value")
            elif re.search(r"\([^)]*\*[^)]*\)", expression):
                capture_kinds.setdefault(name, "address")
            else:
                capture_kinds.setdefault(name, "scalar_or_pointer")
            observer_names = enriched[step - 1].setdefault(
                "observer_capture_names", []
            )
            if name not in observer_names:
                observer_names.append(name)
            if expression.startswith("*") and len(captures) < 4:
                address_expression = expression[1:].strip()
                if _safe_observation_expression(address_expression):
                    address_name = name + "_address"
                    captures.setdefault(address_name, address_expression)
                    capture_kinds.setdefault(address_name, "address")
                    if address_name not in observer_names:
                        observer_names.append(address_name)
    return enriched
