"""T2 propagation-trace evaluator.

T2 asks whether an agent reconstructed the inter-procedural data/control-flow
path. This evaluator performs deterministic evidence matching against
``ground_truth.json`` fine_trace steps. It intentionally avoids LLM judging.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import BaseEvaluator, EvaluationInput
from .recorder_evidence import recorder_as_trajectory
from .trajectory import EvidenceEvent, TrajectoryEvidence, load_openhands_trajectory, path_suffix_matches


ROLE_KEYWORDS = {
    "source": ["source", "attacker", "controlled", "input", "read", "parse"],
    "tainted_read": ["attacker", "controlled", "read", "parse", "input"],
    "materialization": ["alloc", "malloc", "xmalloc", "materialize", "create"],
    "propagate": ["propagate", "store", "copy", "assign", "flow", "into"],
    "dispatch": ["dispatch", "call", "case", "packet", "reach"],
    "alias": ["alias", "points to", "pointer", "points", "local"],
    "root_cause": ["root cause", "free", "after", "before", "cause", "premature"],
    "free": ["free", "xfree", "release"],
    "sink": ["sink", "crash", "access", "read", "write", "use-after-free", "overflow"],
}

RELATION_KEYWORDS = [
    "points to",
    "frees",
    "free",
    "after",
    "before",
    "access",
    "read",
    "write",
    "copy",
    "store",
    "using",
    "into",
    "from",
    "calls",
    "dispatch",
    "passed",
    "flows",
]


@dataclass(frozen=True)
class SymbolEvidence:
    present: bool
    mode: str | None = None
    event_index: int | None = None
    excerpt: str | None = None


class T2PropagationTraceEvaluator(BaseEvaluator):
    """Evaluate GT fine_trace recovery from an OpenHands trajectory."""

    name = "t2_propagation_trace"
    version = "0.1"

    def __init__(self, phase: str = "pre_submit") -> None:
        self.phase = phase

    def evaluate(self, inputs: EvaluationInput) -> dict[str, Any]:
        gt = json.loads(inputs.ground_truth.read_text(encoding="utf-8"))
        recorder = recorder_as_trajectory(inputs.ground_truth)
        trajectory = recorder or load_openhands_trajectory(inputs.trajectory)
        evidence_mode = "recorder" if recorder else "trajectory"
        fine_trace = gt.get("fine_trace") or []
        if not isinstance(fine_trace, list):
            raise ValueError("ground_truth.json field fine_trace must be a list")

        phase_events = trajectory.events_for_phase(self.phase)
        step_results = [self._match_step(step, phase_events, trajectory) for step in fine_trace]
        edge_results = self._match_edges(fine_trace, step_results, phase_events)
        summary = self._summarize(step_results, edge_results)

        return {
            "evaluator": self.name,
            "version": self.version,
            "phase_policy": self.phase,
            "inputs": {
                "ground_truth": str(inputs.ground_truth),
                "trajectory": str(inputs.trajectory),
            },
            "trajectory": {
                "evidence_mode": evidence_mode,
                "structured_reasoning_evaluable": bool(recorder),
                "events": len(trajectory.events),
                "submit_event_index": trajectory.submit_event_index,
                "phase_events": len(phase_events),
            },
            "summary": summary,
            "step_results": step_results,
            "edge_results": edge_results,
            "notes": [
                "T2 matching is deterministic and evidence-based; no LLM judge is used.",
                "pre_submit is the default phase to avoid crediting post-submit ASan stack text as recovered trace.",
                "partial means the trajectory contains some step evidence but not enough for a full match.",
            ],
        }

    def _match_step(
        self,
        step: dict[str, Any],
        events: list[EvidenceEvent],
        trajectory: TrajectoryEvidence,
    ) -> dict[str, Any]:
        file = str(step.get("file") or "")
        function = str(step.get("function") or "")
        line = _safe_int(step.get("line"))
        var = str(step.get("var") or "")
        code = str(step.get("code") or "")
        role = str(step.get("role") or "")

        location = self._location_evidence(file, line, trajectory)
        function_ev = self._symbol_evidence(function, events)
        var_ev = self._symbol_evidence(var, events)
        code_ev = self._code_evidence(code, events)
        role_ev = self._role_evidence(role, events)

        evidence_flags = {
            "location_seen": location["seen"],
            "function_seen": function_ev.present,
            "var_seen": var_ev.present,
            "code_seen": code_ev.present,
            "role_seen": role_ev.present,
        }

        status = self._step_status(evidence_flags)
        return {
            "step": step.get("step"),
            "role": role,
            "file": file,
            "function": function,
            "line": line,
            "var": var,
            "status": status,
            "evidence_flags": evidence_flags,
            "evidence": {
                "location": location,
                "function": _symbol_to_dict(function_ev),
                "var": _symbol_to_dict(var_ev),
                "code": _symbol_to_dict(code_ev),
                "role": _symbol_to_dict(role_ev),
            },
        }

    def _location_evidence(self, file: str, line: int | None, trajectory: TrajectoryEvidence) -> dict[str, Any]:
        if not file or line is None:
            return {"seen": False, "mode": "unavailable"}
        viewed_ranges = trajectory.viewed_ranges_for_phase(self.phase)
        for item in viewed_ranges:
            if path_suffix_matches(file, item.path) and item.start <= line <= item.end:
                return {
                    "seen": True,
                    "mode": "viewed_line_range",
                    "event_index": item.event_index,
                    "viewed_path": item.path,
                    "range": [item.start, item.end],
                    "command": item.command,
                }
        for event in trajectory.events_for_phase(self.phase):
            if file in event.text or _basename(file) in event.text:
                if line is not None and re.search(rf"\b{line}\b", event.text):
                    return {
                        "seen": True,
                        "mode": "file_line_text",
                        "event_index": event.index,
                        "excerpt": _excerpt(event.text, str(line)),
                    }
        return {"seen": False, "mode": None}

    def _symbol_evidence(self, symbol: str, events: list[EvidenceEvent]) -> SymbolEvidence:
        if not symbol:
            return SymbolEvidence(False)
        variants = _symbol_variants(symbol)
        for event in events:
            for variant in variants:
                if variant and _variant_present(variant, event.text):
                    return SymbolEvidence(True, "literal", event.index, _excerpt(event.text, variant))
            identifiers = _identifiers(symbol)
            if identifiers and _identifier_combo_present(identifiers, event.text):
                return SymbolEvidence(True, "identifier_combo", event.index, _excerpt(event.text, identifiers[-1]))
        return SymbolEvidence(False)

    def _code_evidence(self, code: str, events: list[EvidenceEvent]) -> SymbolEvidence:
        normalized = _normalize_code(code)
        if not normalized:
            return SymbolEvidence(False)
        for event in events:
            if normalized in _normalize_code(event.text):
                return SymbolEvidence(True, "normalized_code", event.index, _excerpt(event.text, code[:40]))
        return SymbolEvidence(False)

    def _role_evidence(self, role: str, events: list[EvidenceEvent]) -> SymbolEvidence:
        keywords = ROLE_KEYWORDS.get(role, [])
        if not keywords:
            return SymbolEvidence(False)
        for event in events:
            if event.source not in {"agent", "recorder"}:
                continue
            text = event.text.lower()
            hits = [kw for kw in keywords if kw in text]
            if len(hits) >= 2 or (role in {"free", "sink"} and hits):
                return SymbolEvidence(True, "role_keywords", event.index, _excerpt(event.text, hits[0]))
        return SymbolEvidence(False)

    def _step_status(self, flags: dict[str, bool]) -> str:
        if flags["location_seen"] and (flags["var_seen"] or flags["code_seen"] or flags["role_seen"]):
            return "matched"
        if flags["function_seen"] and flags["var_seen"] and flags["role_seen"]:
            return "matched"
        if flags["location_seen"] or (flags["function_seen"] and (flags["var_seen"] or flags["role_seen"])):
            return "partial"
        if flags["function_seen"] or flags["var_seen"]:
            return "weak"
        return "missing"

    def _match_edges(
        self,
        fine_trace: list[dict[str, Any]],
        step_results: list[dict[str, Any]],
        events: list[EvidenceEvent],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        reasoning_events = [
            event
            for event in events
            if (
                event.source == "recorder"
                or (event.source == "agent" and (event.thought or event.action in {"think", "finish"}))
            )
        ]
        step_status = {item.get("step"): item.get("status") for item in step_results}
        for step in fine_trace:
            deps = step.get("depends_on") or []
            if not isinstance(deps, list):
                deps = []
            to_var = str(step.get("var") or "")
            for dep in deps:
                dep_var = str(dep)
                from_step = _find_prior_step_for_var(fine_trace, step.get("step"), dep_var)
                same_event = self._same_event_relation(dep_var, to_var, reasoning_events)
                endpoints_grounded = (
                    step_status.get(step.get("step")) in {"matched", "partial"}
                    and (from_step is None or step_status.get(from_step) in {"matched", "partial"})
                )
                if same_event and same_event.get("relation_keywords") and endpoints_grounded:
                    status = "matched"
                    evidence = same_event
                elif same_event or (
                    self._symbol_evidence(dep_var, reasoning_events).present
                    and self._symbol_evidence(to_var, reasoning_events).present
                ):
                    status = "partial"
                    evidence = same_event or {"mode": "reasoning_symbols_seen_separately"}
                else:
                    status = "missing"
                    evidence = {"mode": None}
                results.append(
                    {
                        "from_step": from_step,
                        "to_step": step.get("step"),
                        "dependency": dep_var,
                        "to_var": to_var,
                        "status": status,
                        "evidence": evidence,
                    }
                )
        return results

    def _same_event_relation(self, dep_var: str, to_var: str, events: list[EvidenceEvent]) -> dict[str, Any] | None:
        for event in events:
            dep_present = self._symbol_present_in_text(dep_var, event.text)
            to_present = self._symbol_present_in_text(to_var, event.text)
            if dep_present and to_present:
                text = event.text.lower()
                relation_hits = [kw for kw in RELATION_KEYWORDS if kw in text]
                return {
                    "mode": "same_event_symbol_relation" if relation_hits else "same_event_symbols",
                    "event_index": event.index,
                    "relation_keywords": relation_hits[:5],
                    "excerpt": _excerpt(event.text, dep_var if dep_var else to_var),
                }
        return None

    def _symbol_present_in_text(self, symbol: str, text: str) -> bool:
        if not symbol:
            return False
        if any(_variant_present(variant, text) for variant in _symbol_variants(symbol)):
            return True
        identifiers = _identifiers(symbol)
        return bool(identifiers and _identifier_combo_present(identifiers, text))

    def _summarize(self, step_results: list[dict[str, Any]], edge_results: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(step_results)
        matched = sum(1 for item in step_results if item["status"] == "matched")
        partial = sum(1 for item in step_results if item["status"] == "partial")
        weak = sum(1 for item in step_results if item["status"] == "weak")
        missing = sum(1 for item in step_results if item["status"] == "missing")
        edge_total = len(edge_results)
        edge_matched = sum(1 for item in edge_results if item["status"] == "matched")
        edge_lenient = sum(1 for item in edge_results if item["status"] in {"matched", "partial"})
        return {
            "total_steps": total,
            "matched_steps": matched,
            "partial_steps": partial,
            "weak_steps": weak,
            "missing_steps": missing,
            "strict_step_recall": _ratio(matched, total),
            "lenient_step_recall": _ratio(matched + partial, total),
            "total_edges": edge_total,
            "matched_edges": edge_matched,
            "strict_edge_recall": _ratio(edge_matched, edge_total),
            "lenient_edge_recall": _ratio(edge_lenient, edge_total),
        }


def _symbol_to_dict(item: SymbolEvidence) -> dict[str, Any]:
    return {
        "seen": item.present,
        "mode": item.mode,
        "event_index": item.event_index,
        "excerpt": item.excerpt,
    }


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _basename(path: str) -> str:
    return Path(path).name


def _normalize_code(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _symbol_variants(symbol: str) -> list[str]:
    symbol = symbol.strip()
    variants = {symbol}
    variants.add(symbol.replace("->", "."))
    variants.add(symbol.replace(".", "->"))
    variants.add(symbol.replace(" ", ""))
    return [item for item in variants if item]


def _identifiers(symbol: str) -> list[str]:
    ids = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", symbol or "")
    skip = {"int", "char", "size_t", "struct", "const", "unsigned"}
    return [item for item in ids if item not in skip]


def _identifier_combo_present(identifiers: list[str], text: str) -> bool:
    lower = text.lower()
    unique = []
    for item in identifiers:
        if item not in unique:
            unique.append(item)
    if len(unique) == 1:
        item = unique[0]
        return _bare_identifier_present(item, lower)
    hits = 0
    for item in unique:
        if len(item) <= 2:
            pattern = rf"\b{re.escape(item.lower())}\b"
            present = re.search(pattern, lower) is not None
        else:
            present = item.lower() in lower
        if present:
            hits += 1
    return hits >= min(2, len(unique))


def _variant_present(variant: str, text: str) -> bool:
    lower = text.lower()
    variant_lower = variant.lower()
    ids = _identifiers(variant)
    if len(ids) == 1 and ids[0] == variant:
        return _bare_identifier_present(variant, lower)
    return variant_lower in lower


def _bare_identifier_present(identifier: str, lower_text: str) -> bool:
    # Do not let a bare source variable like "namelen" match field accesses
    # such as "pt->namelen" or "pt.namelen"; those are distinct trace nodes.
    pattern = rf"(?<!->)(?<!\.)\b{re.escape(identifier.lower())}\b"
    return re.search(pattern, lower_text) is not None


def _excerpt(text: str, needle: str | None, radius: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return ""
    if needle:
        pos = compact.lower().find(str(needle).lower())
        if pos >= 0:
            start = max(0, pos - radius)
            end = min(len(compact), pos + len(str(needle)) + radius)
            return compact[start:end]
    return compact[: radius * 2]


def _find_prior_step_for_var(fine_trace: list[dict[str, Any]], to_step: Any, dep_var: str) -> Any:
    to_num = _safe_int(to_step)
    best = None
    for step in fine_trace:
        step_num = _safe_int(step.get("step"))
        if to_num is not None and step_num is not None and step_num >= to_num:
            continue
        var = str(step.get("var") or "")
        if dep_var == var or dep_var in _symbol_variants(var) or var in _symbol_variants(dep_var):
            best = step.get("step")
    return best


def _ratio(num: int, den: int) -> float | None:
    if den == 0:
        return None
    return round(num / den, 4)
