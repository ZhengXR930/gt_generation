#!/usr/bin/env python3
"""Deterministic vulnerability-logic scoring against verified invariants.

This evaluator compares ``analysis.json``'s embedded ``vuln_logic`` object to
the GT causal graph in ``verified_invariants.json``.  It does not read
free-form explanations and does not call an LLM judge.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluator.field_bindings import parse_binding


REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"

OPS = {"eq", "ne", "lt", "le", "gt", "ge", "same_object"}
FLIPPED_OPS = {"lt": "gt", "gt": "lt", "le": "ge", "ge": "le", "eq": "eq", "ne": "ne"}
IDENTITY_OPS = {"eq", "same_object"}
EDGE_TYPES = {"data", "control", "order"}
OBLIGATION_ROLES = {"root_cause", "root_obligation", "obligation"}
DEFAULT_LINE_TOLERANCE = 5

_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:(?:->|\.)[A-Za-z_][A-Za-z0-9_]*)*"
)
_NUMERIC_SUFFIX_RE = re.compile(r"(?<=\d)[uUlLfF]+")
_SIZEOF_STRING_RE = re.compile(r"""^sizeof\(\s*((?:"(?:\\.|[^"\\])*")|(?:'(?:\\.|[^'\\])*'))\s*\)$""")
_OUTER_PARENS_RE = re.compile(r"^\((.*)\)$", re.DOTALL)
_CXX_NAMED_CAST_RE = re.compile(
    r"^(?:const|static|reinterpret|dynamic)_cast\s*<[^<>]+>\s*\((.*)\)$",
    re.DOTALL,
)
_SIMPLE_CAST_RE = re.compile(
    r"\(\s*(?:const\s+|volatile\s+|struct\s+|enum\s+|union\s+|"
    r"[A-Za-z_][A-Za-z0-9_]*\s+)*[A-Za-z_][A-Za-z0-9_]*(?:\s*\*)+\s*\)"
)


@dataclass(frozen=True)
class Location:
    file: str
    function: str
    line: int | None


class ExpressionNormalizer:
    """Build deterministic expression equality keys for one GT sample."""

    _TIER_PRIORITY = {
        "exact": 0,
        "structural": 1,
        "alias": 2,
        "constant": 3,
        "partial": 4,
    }

    def __init__(self, bindings: dict[str, Any]):
        self.alias_index: dict[str, set[str]] = {}
        for key, value in bindings.items():
            binding = parse_binding(str(key), value)
            class_id = str(binding.canonical or binding.key or key)
            candidates: list[Any] = [binding.key, binding.canonical, binding.expr]
            candidates.extend(binding.aliases)
            for candidate in candidates:
                for normalized in self._structural_keys(candidate):
                    self.alias_index.setdefault(normalized, set()).add(class_id)
                constant = self._constant_key(candidate)
                if constant:
                    self.alias_index.setdefault(constant, set()).add(class_id)

    def keys(self, expression: Any) -> dict[str, str]:
        keys: dict[str, str] = {}
        raw = str(expression or "").strip()
        if raw:
            keys[f"raw:{raw}"] = "exact"
        for normalized in self._structural_keys(raw):
            keys.setdefault(normalized, "structural")
            lowered = normalized.lower()
            if lowered != normalized:
                keys.setdefault(lowered, "structural")
            for class_id in self.alias_index.get(normalized, set()):
                keys.setdefault(f"alias:{class_id}", "alias")
            for class_id in self.alias_index.get(lowered, set()):
                keys.setdefault(f"alias:{class_id}", "alias")
        constant = self._constant_key(raw)
        if constant:
            keys.setdefault(constant, "constant")
            for class_id in self.alias_index.get(constant, set()):
                keys.setdefault(f"alias:{class_id}", "alias")
        return keys

    def compare(self, left: Any, right: Any) -> dict[str, Any]:
        left_keys = self.keys(left)
        right_keys = self.keys(right)
        common = set(left_keys) & set(right_keys)
        if not common:
            partial = self._partial_compare(left, right)
            if partial["matched"]:
                return partial
            return {
                "matched": False,
                "norm_tier": "unresolved",
                "left": left,
                "right": right,
            }
        key = min(
            common,
            key=lambda item: min(
                self._TIER_PRIORITY[left_keys[item]],
                self._TIER_PRIORITY[right_keys[item]],
            ),
        )
        tier = min(
            (left_keys[key], right_keys[key]),
            key=lambda item: self._TIER_PRIORITY[item],
        )
        return {
            "matched": True,
            "norm_tier": tier,
            "left": left,
            "right": right,
            "matched_key": key,
        }

    def _partial_compare(self, left: Any, right: Any) -> dict[str, Any]:
        """Return a non-exact subexpression match.

        This is deliberately lower-tier than alias/constant matching and is not
        used by hard all-operands scores.  It captures cases such as GT
        ``buf`` vs agent ``fn(buf, len)`` or GT ``_starttab`` vs agent
        ``_starttab[o]`` for partial reasoning diagnostics.
        """
        left_keys = self._partial_keys(left)
        right_keys = self._partial_keys(right)
        common = left_keys & right_keys
        if not common:
            return {
                "matched": False,
                "norm_tier": "unresolved",
                "left": left,
                "right": right,
            }
        return {
            "matched": True,
            "norm_tier": "partial",
            "left": left,
            "right": right,
            "matched_key": sorted(common, key=len)[-1],
        }

    @classmethod
    def _partial_keys(cls, value: Any) -> set[str]:
        text = str(value or "").strip()
        keys: set[str] = set()
        for structural in cls._structural_keys(text):
            raw = structural[len("struct:"):] if structural.startswith("struct:") else structural
            if raw:
                keys.add(f"partial:{raw}")
        no_space = _SPACE_RE.sub("", text).replace("->", ".")
        for token in _TOKEN_RE.findall(no_space):
            if not token:
                continue
            keys.add(f"partial:{token}")
            if "." in token:
                parts = [part for part in token.split(".") if part]
                keys.add(f"partial:{parts[-1]}")
                for index in range(1, len(parts)):
                    keys.add(f"partial:{'.'.join(parts[:index])}")
        return keys

    @classmethod
    def _structural_keys(cls, value: Any) -> set[str]:
        text = str(value or "").strip()
        if not text:
            return set()
        no_space = _SPACE_RE.sub("", text)
        forms = {no_space}
        stripped = cls._strip_outer_parens(no_space)
        forms.add(stripped)
        no_cast = cls._strip_cast_wrappers(stripped)
        forms.add(no_cast)
        arrow = no_cast.replace("->", ".")
        forms.add(arrow)
        # Pointer/address syntax often differs between GT aliases and agent
        # claims.  Keep this as an additional key, not the only key, so precise
        # forms still win when present.
        pointer_neutral = arrow.replace("*", "").replace("&", "")
        forms.add(pointer_neutral)
        return {f"struct:{item}" for item in forms if item}

    @staticmethod
    def _strip_outer_parens(value: str) -> str:
        text = value
        while True:
            match = _OUTER_PARENS_RE.match(text)
            if not match:
                return text
            inner = match.group(1)
            depth = 0
            balanced = True
            for index, char in enumerate(inner):
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth < 0 or (depth == 0 and index != len(inner) - 1):
                        balanced = False
                        break
            if not balanced:
                return text
            text = inner

    @classmethod
    def _strip_cast_wrappers(cls, value: str) -> str:
        text = value
        while True:
            match = _CXX_NAMED_CAST_RE.match(text)
            if match:
                text = cls._strip_outer_parens(match.group(1))
                continue
            no_c_cast = _SIMPLE_CAST_RE.sub("", text)
            if no_c_cast != text:
                text = cls._strip_outer_parens(no_c_cast)
                continue
            return text

    @classmethod
    def _constant_key(cls, value: Any) -> str | None:
        text = _SPACE_RE.sub("", str(value or "").strip())
        if not text:
            return None
        sizeof_value = cls._sizeof_string_value(text)
        if sizeof_value is not None:
            return f"const:{sizeof_value}"
        arithmetic_value = cls._safe_int_eval(text)
        if arithmetic_value is not None:
            return f"const:{arithmetic_value}"
        return None

    @staticmethod
    def _sizeof_string_value(text: str) -> int | None:
        match = _SIZEOF_STRING_RE.match(text)
        if not match:
            return None
        literal = match.group(1)
        try:
            decoded = ast.literal_eval(literal)
        except (SyntaxError, ValueError):
            return None
        if isinstance(decoded, str):
            return len(decoded.encode("utf-8")) + 1
        if isinstance(decoded, bytes):
            return len(decoded) + 1
        return None

    @staticmethod
    def _safe_int_eval(text: str) -> int | None:
        cleaned = _NUMERIC_SUFFIX_RE.sub("", text)
        if re.fullmatch(r"[-+]?(?:0x[0-9A-Fa-f]+|\d+)", cleaned):
            try:
                return int(cleaned, 0)
            except ValueError:
                return None
        if not re.fullmatch(r"[0-9A-Fa-fxX()+\-*/%<>&|~^]+", cleaned):
            return None
        try:
            tree = ast.parse(cleaned, mode="eval")
        except SyntaxError:
            return None
        allowed_nodes = (
            ast.Expression,
            ast.BinOp,
            ast.UnaryOp,
            ast.Constant,
            ast.Num,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.FloorDiv,
            ast.Div,
            ast.Mod,
            ast.LShift,
            ast.RShift,
            ast.BitOr,
            ast.BitAnd,
            ast.BitXor,
            ast.Invert,
            ast.USub,
            ast.UAdd,
        )
        if any(not isinstance(node, allowed_nodes) for node in ast.walk(tree)):
            return None
        try:
            value = eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, {})
        except Exception:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return None


def score_vuln_logic(
    sample_id: str,
    logic: dict[str, Any],
    *,
    gt_dir: Path | None = None,
    line_tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> dict[str, Any]:
    """Score one model ``vuln_logic`` object against one GT sample."""
    gt_dir = gt_dir or (GT_RESULTS / sample_id)
    gt = _load_json(gt_dir / "ground_truth.json")
    invariants = _load_json(gt_dir / "verified_invariants.json")
    bindings = (_load_json(gt_dir / "field_bindings.json").get("bindings") or {})
    normalizer = ExpressionNormalizer(bindings if isinstance(bindings, dict) else {})

    nodes = [item for item in invariants.get("nodes", []) if isinstance(item, dict)]
    edges = [item for item in invariants.get("edges", []) if isinstance(item, dict)]
    node_by_id = {
        str(item.get("invariant_id") or ""): item
        for item in nodes
        if str(item.get("invariant_id") or "")
    }
    root_id = str((invariants.get("root_cause_criterion") or {}).get("invariant_id") or "")
    source = _first_node(nodes, {"source"})
    obligation = node_by_id.get(root_id) or _first_node(nodes, OBLIGATION_ROLES)
    sink = _first_node(nodes, {"sink"})
    source_chain = _chain_anchor_node(gt.get("source"), source, "source")
    obligation_chain = _chain_anchor_node(
        gt.get("root_cause"), obligation, "root_cause"
    )
    sink_chain = _chain_anchor_node(gt.get("sink"), sink, "sink")

    source_result = _match_point(
        source_chain,
        logic.get("source"),
        normalizer,
        line_tolerance=line_tolerance,
        require_relation=False,
        allow_flipped_relation=False,
    )
    obligation_result = _match_point(
        obligation_chain,
        logic.get("root_cause"),
        normalizer,
        line_tolerance=line_tolerance,
        require_relation=True,
        allow_flipped_relation=True,
    )
    sink_result = _match_point(
        sink_chain,
        logic.get("sink"),
        normalizer,
        line_tolerance=line_tolerance,
        require_relation=True,
        allow_flipped_relation=True,
    )
    propagation_rows = [
        _match_edge(edge, node_by_id, logic.get("propagation"), normalizer, line_tolerance)
        for edge in edges
        if isinstance(edge, dict)
    ]
    chain_rows = _match_propagation_chains(
        source_chain,
        obligation_chain,
        sink_chain,
        edges,
        node_by_id,
        gt,
        logic.get("propagation"),
        normalizer,
        line_tolerance,
    )
    chain_available = [row for row in chain_rows if row.get("available") is not False]
    loc_score = _rate(row.get("loc_hit") is True for row in chain_available)
    partial_score = _rate(row.get("partial_hit") is True for row in chain_available)
    carrier_score = _rate(row.get("carrier_hit") is True for row in chain_available)
    carrier_partial_score = _rate(row.get("carrier_partial_hit") is True for row in chain_available)
    full_score = _rate(row.get("full_hit") is True for row in chain_available)
    edge_endpoint_score = _rate(row.get("loc_hit") is True for row in propagation_rows)
    edge_type_score = _rate(row.get("type_hit") is True for row in propagation_rows)
    edge_carrier_score = _rate(row.get("carrier_hit") is True for row in propagation_rows)
    edge_carrier_partial_score = _rate(row.get("carrier_partial_hit") is True for row in propagation_rows)
    edge_full_score = _rate(row.get("full_hit") is True for row in propagation_rows)
    all_norm_counts = _norm_counts([source_result, obligation_result, sink_result] + propagation_rows)
    matched_location_norm_counts = _norm_counts_for_matched_locations(
        [source_result, obligation_result, sink_result], propagation_rows
    )
    source_scores = _point_scores(source_result, require_relation=False)
    obligation_scores = _point_scores(obligation_result, require_relation=True)
    sink_scores = _point_scores(sink_result, require_relation=True)
    propagation_scores = {
        "mode": "semantic_edge_unit",
        "loc": loc_score,
        "partial": partial_score,
        "full": full_score,
        "edge_diagnostics": {
            "loc": edge_endpoint_score,
            "type": edge_type_score,
            "carrier": edge_carrier_score,
            "carrier_partial": edge_carrier_partial_score,
            "full": edge_full_score,
            "exact_full": _rate(row.get("exact_full_hit") is True for row in propagation_rows),
        },
    }
    return {
        "evaluation_protocol": "vuln-logic-invariant-reasoning-v2",
        "sample_id": sample_id,
        "line_tolerance": line_tolerance,
        "dimension_scores": {
            "source": source_scores,
            "safety_obligation": obligation_scores,
            "sink": sink_scores,
            "propagation": propagation_scores,
        },
        "source_loc_score": source_scores["loc"],
        "source_partial_score": source_scores["partial"],
        "source_full_score": source_scores["full"],
        "obligation_loc_score": obligation_scores["loc"],
        "obligation_partial_score": obligation_scores["partial"],
        "obligation_full_score": obligation_scores["full"],
        "sink_loc_score": sink_scores["loc"],
        "sink_partial_score": sink_scores["partial"],
        "sink_full_score": sink_scores["full"],
        "propagation_loc_score": loc_score,
        "propagation_partial_score": partial_score,
        "propagation_full_score": full_score,
        "propagation_gap": (
            loc_score - full_score
            if loc_score is not None and full_score is not None
            else None
        ),
        "propagation_total": len(chain_available),
        "propagation_loc_hits": sum(row.get("loc_hit") is True for row in chain_available),
        "propagation_partial_hits": sum(row.get("partial_hit") is True for row in chain_available),
        "propagation_type_hits": sum(row.get("type_hit") is True for row in chain_available),
        "propagation_carrier_hits": sum(row.get("carrier_hit") is True for row in chain_available),
        "propagation_carrier_partial_hits": sum(row.get("carrier_partial_hit") is True for row in chain_available),
        "propagation_exact_full_hits": sum(row.get("exact_full_hit") is True for row in chain_available),
        "propagation_full_hits": sum(row.get("full_hit") is True for row in chain_available),
        "propagation_edge_total": len(propagation_rows),
        "propagation_edge_loc_score": edge_endpoint_score,
        "propagation_edge_type_score": edge_type_score,
        "propagation_edge_carrier_score": edge_carrier_score,
        "propagation_edge_carrier_partial_score": edge_carrier_partial_score,
        "propagation_edge_full_score": edge_full_score,
        "propagation_edge_loc_hits": sum(row.get("loc_hit") is True for row in propagation_rows),
        "propagation_edge_type_hits": sum(row.get("type_hit") is True for row in propagation_rows),
        "propagation_edge_carrier_hits": sum(row.get("carrier_hit") is True for row in propagation_rows),
        "propagation_edge_carrier_partial_hits": sum(row.get("carrier_partial_hit") is True for row in propagation_rows),
        "propagation_edge_exact_full_hits": sum(row.get("exact_full_hit") is True for row in propagation_rows),
        "propagation_edge_full_hits": sum(row.get("full_hit") is True for row in propagation_rows),
        "norm_tier_counts": matched_location_norm_counts,
        "norm_unresolved_rate": (
            matched_location_norm_counts.get("unresolved", 0) / sum(matched_location_norm_counts.values())
            if sum(matched_location_norm_counts.values()) else None
        ),
        "norm_tier_counts_all": all_norm_counts,
        "norm_unresolved_rate_all": (
            all_norm_counts.get("unresolved", 0) / sum(all_norm_counts.values())
            if sum(all_norm_counts.values()) else None
        ),
        "diagnostics": {
            "source": source_result,
            "safety_obligation": obligation_result,
            "sink": sink_result,
            "propagation": chain_rows,
            "propagation_edges": propagation_rows,
        },
    }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _first_node(nodes: list[dict[str, Any]], roles: set[str]) -> dict[str, Any] | None:
    for node in nodes:
        if str(node.get("role") or "") in roles:
            return node
    return None


def _score(result: dict[str, Any]) -> int | None:
    if result.get("available") is False:
        return None
    return int(result.get("hit") is True)


def _bool_score(value: Any, *, available: bool = True) -> int | None:
    if not available:
        return None
    return int(value is True)


def _point_scores(result: dict[str, Any], *, require_relation: bool) -> dict[str, int | None]:
    available = result.get("available") is not False
    relation = result.get("relation_match") if isinstance(result.get("relation_match"), dict) else {}
    operand = result.get("operand_match") if isinstance(result.get("operand_match"), dict) else {}
    partial = bool(
        result.get("location_match") is True
        and operand.get("partial_matched") is True
    )
    relation_required = bool(require_relation and relation.get("required") is not False)
    full = bool(
        partial
        and (not relation_required or relation.get("matched") is True)
    )
    return {
        "loc": _bool_score(result.get("location_match"), available=available),
        "partial": None if not require_relation else _bool_score(partial, available=available),
        "full": _bool_score(full, available=available),
    }


def _rate(values: Any) -> float | None:
    items = list(values)
    if not items:
        return None
    return sum(bool(item) for item in items) / len(items)


def _loc(item: Any) -> Location:
    value = item if isinstance(item, dict) else {}
    line = value.get("line")
    return Location(
        file=str(value.get("file") or ""),
        function=str(value.get("function") or ""),
        line=line if isinstance(line, int) else None,
    )


def _norm_path(value: Any) -> str:
    path = str(value or "").replace("\\", "/").strip()
    for prefix in ("repo-vul/src-vul/", "src-vul/", "./"):
        while path.startswith(prefix):
            path = path[len(prefix):]
    return re.sub(r"/+", "/", path)


def _file_matches(left: Any, right: Any) -> bool:
    a, b = _norm_path(left), _norm_path(right)
    return bool(a and b and (a == b or a.endswith("/" + b) or b.endswith("/" + a)))


def _norm_function(value: Any) -> str:
    text = str(value or "").strip().split("(", 1)[0].strip()
    parts = text.split()
    if parts:
        text = parts[-1]
    return _SPACE_RE.sub("", text)


def _location_matches(gt: Location, agent: Location, tolerance: int) -> bool:
    if gt.line is None or agent.line is None:
        line_match = gt.line is None and agent.line is None
    else:
        line_match = abs(gt.line - agent.line) <= tolerance
    return (
        _file_matches(gt.file, agent.file)
        and _norm_function(gt.function) == _norm_function(agent.function)
        and line_match
    )


def _match_point(
    gt_node: dict[str, Any] | None,
    agent_point: Any,
    normalizer: ExpressionNormalizer,
    *,
    line_tolerance: int,
    require_relation: bool,
    allow_flipped_relation: bool,
) -> dict[str, Any]:
    if gt_node is None:
        return {"available": False, "hit": None, "reason": "gt_node_missing"}
    if not isinstance(agent_point, dict):
        return {
            "available": True,
            "hit": False,
            "gt": _node_summary(gt_node),
            "reason": "agent_point_missing",
        }
    loc_match = _location_matches(_loc(gt_node), _loc(agent_point), line_tolerance)
    operand_match = _match_all_operands(
        gt_node.get("operands") or [], agent_point.get("operands") or [], normalizer
    )
    relation_match = {"required": False, "matched": None}
    if require_relation:
        relation_match = _match_relation(
            gt_node.get("relation"),
            agent_point.get("relation"),
            normalizer,
            allow_flipped=allow_flipped_relation,
        )
    relation_required = bool(require_relation and relation_match.get("required") is not False)
    exact_hit = bool(
        loc_match
        and operand_match.get("matched")
        and (not relation_required or relation_match.get("matched"))
    )
    relation_operands_match = bool(
        isinstance(relation_match, dict)
        and relation_match.get("operand_matched") is True
    )
    semantic_operand_hit = bool(
        operand_match.get("partial_matched")
        or (relation_required and relation_operands_match)
    )
    semantic_hit = bool(
        loc_match
        and semantic_operand_hit
        and (not relation_required or relation_match.get("matched"))
    )
    return {
        "available": True,
        "hit": semantic_hit,
        "exact_hit": exact_hit,
        "location_match": loc_match,
        "operand_match": operand_match,
        "relation_match": relation_match,
        "gt": _node_summary(gt_node),
        "agent": _point_summary(agent_point),
    }


def _match_propagation_chains(
    source: dict[str, Any] | None,
    obligation: dict[str, Any] | None,
    sink: dict[str, Any] | None,
    gt_edges: list[dict[str, Any]],
    node_by_id: dict[str, dict[str, Any]],
    gt: dict[str, Any],
    agent_edges: Any,
    normalizer: ExpressionNormalizer,
    line_tolerance: int,
) -> list[dict[str, Any]]:
    chains = [
        ("source_to_obligation", source, obligation),
        ("obligation_to_sink", obligation, sink),
        ("source_to_sink", source, sink),
    ]
    chain_nodes = {
        _node_id(item): item
        for item in (source, obligation, sink)
        if _node_id(item)
    }
    graph_nodes = dict(node_by_id)
    graph_nodes.update(chain_nodes)
    graph_nodes.update(_fine_trace_nodes(gt))
    gt_graph_edges = _graph_edges_from_gt(gt_edges, node_by_id)
    gt_graph_edges.extend(_graph_edges_from_fine_trace(gt, chain_nodes, line_tolerance))
    agent_graph_edges = _graph_edges_from_agent(agent_edges, graph_nodes, line_tolerance)
    rows = []
    for name, start_node, end_node in chains:
        start_id = _node_id(start_node)
        end_id = _node_id(end_node)
        if not start_id or not end_id:
            rows.append({
                "chain": name,
                "available": False,
                "loc_hit": None,
                "partial_hit": None,
                "full_hit": None,
                "reason": "gt_chain_endpoint_missing",
            })
            continue
        gt_chain_edges = _edges_on_any_path(gt_graph_edges, start_id, end_id)
        if not gt_chain_edges:
            rows.append({
                "chain": name,
                "available": False,
                "loc_hit": None,
                "partial_hit": None,
                "full_hit": None,
                "reason": "gt_chain_path_missing",
                "gt": {
                    "from": _node_summary(start_node or {}),
                    "to": _node_summary(end_node or {}),
                },
            })
            continue
        agent_chain_edges = _agent_edges_in_gt_segment(
            agent_graph_edges, gt_chain_edges, start_id, end_id
        )
        loc_hit = bool(agent_chain_edges)
        gt_types = _edge_types(gt_chain_edges)
        agent_types = _edge_types(agent_chain_edges)
        # A semantic propagation unit is not a full data-flow proof.  The GT
        # fine trace may contain several data/control/order facts along the
        # path, while a model claim usually states the key edge it understood.
        # Therefore type/carrier/relation matching is existential within the
        # located chain, with recall details preserved for diagnostics.
        type_hit = bool(gt_types and agent_types and (gt_types & agent_types))
        gt_carriers = _edge_carriers(gt_chain_edges)
        agent_carriers = _edge_carriers(agent_chain_edges)
        carrier_match = _match_any_operand(gt_carriers, agent_carriers, normalizer)
        relation_match = _match_chain_relations(gt_chain_edges, agent_chain_edges, normalizer)
        relation_required = relation_match.get("required") is True
        relation_hit = (
            relation_match.get("matched") is True if relation_required else True
        )
        partial_hit = bool(loc_hit and carrier_match.get("partial_matched"))
        exact_full_hit = bool(
            loc_hit
            and carrier_match.get("matched")
            and relation_hit
        )
        full_hit = bool(partial_hit and relation_hit)
        rows.append({
            "chain": name,
            "available": True,
            "loc_hit": loc_hit,
            "partial_hit": partial_hit,
            "type_hit": type_hit,
            "carrier_hit": carrier_match.get("matched") is True,
            "carrier_partial_hit": carrier_match.get("partial_matched") is True,
            "relation_hit": (
                relation_match.get("matched") is True
                if relation_required
                else None
            ),
            "exact_full_hit": exact_full_hit,
            "full_hit": full_hit,
            "carrier_match": carrier_match,
            "relation_match": relation_match,
            "gt": {
                "from": _node_summary(start_node or {}),
                "to": _node_summary(end_node or {}),
                "types": sorted(gt_types),
                "carriers": gt_carriers,
                "carrier_match_mode": "any_key_carrier",
                "headline_partial": "loc_plus_carrier_operand",
                "headline_full": "loc_plus_carrier_operand_plus_relation",
                "edges": [_edge_summary(edge) for edge in gt_chain_edges],
            },
            "agent": {
                "types": sorted(agent_types),
                "carriers": agent_carriers,
                "edges": [_edge_summary(edge) for edge in agent_chain_edges],
            },
        })
    return rows


def _node_id(node: dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    return str(node.get("invariant_id") or "")


def _chain_anchor_node(
    anchor: Any,
    fallback: dict[str, Any] | None,
    role: str,
) -> dict[str, Any] | None:
    if not isinstance(anchor, dict):
        return fallback
    node = dict(fallback or {})
    anchor_operands = anchor.get("operands") or _split_anchor_var(anchor.get("var"))
    node.update({
        "invariant_id": _chain_anchor_id(role),
        "role": role,
        "file": node.get("file") or anchor.get("file"),
        "function": node.get("function") or anchor.get("function"),
        "line": node.get("line") if node.get("line") is not None else anchor.get("line"),
        "line_end": node.get("line_end") if node.get("line_end") is not None else anchor.get("line_end"),
        "operands": node.get("operands") or anchor_operands,
        "relation": node.get("relation") or anchor.get("relation"),
        "trace_step": node.get("trace_step") or anchor.get("trace_step"),
        "fine_trace_step": node.get("fine_trace_step") or anchor.get("trace_step"),
        "code": node.get("code") or anchor.get("code"),
    })
    return node


def _chain_anchor_id(role: str) -> str:
    return {
        "source": "__gt_source__",
        "root_cause": "__gt_root_cause__",
        "sink": "__gt_sink__",
    }.get(role, f"__gt_{role}__")


def _split_anchor_var(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def _graph_edges_from_gt(
    edges: list[dict[str, Any]],
    node_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for edge in edges:
        from_id = str(edge.get("from_node") or "")
        to_id = str(edge.get("to_node") or "")
        if from_id in node_by_id and to_id in node_by_id:
            rows.append({
                "from": from_id,
                "to": to_id,
                "type": str(edge.get("type") or ""),
                "carriers": edge.get("operands") or edge.get("via") or [],
                "relation": edge.get("relation"),
                "invariant_id": edge.get("invariant_id"),
            })
    return rows


def _graph_edges_from_fine_trace(
    gt: dict[str, Any],
    chain_nodes: dict[str, dict[str, Any]],
    line_tolerance: int,
) -> list[dict[str, Any]]:
    fine_trace = gt.get("fine_trace")
    if not isinstance(fine_trace, list):
        return []
    step_by_num = {
        item.get("step"): item
        for item in fine_trace
        if isinstance(item, dict) and isinstance(item.get("step"), int)
    }
    node_steps = {
        node_id: _trace_steps_for_node(node, step_by_num, line_tolerance)
        for node_id, node in chain_nodes.items()
    }
    step_nodes: dict[int, list[str]] = {}
    for node_id, steps in node_steps.items():
        for step in steps:
            step_nodes.setdefault(step, []).append(node_id)

    rows: list[dict[str, Any]] = []
    for step_num, item in step_by_num.items():
        deps = item.get("depends_on")
        if not isinstance(deps, list):
            continue
        to_nodes = step_nodes.get(step_num, [])
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            dep_step = dep.get("on")
            if not isinstance(dep_step, int):
                continue
            dep_item = step_by_num.get(dep_step) or {}
            dep_type = str(dep.get("type") or "")
            via = str(dep.get("via") or "").strip()
            carriers = [via] if via else []
            for from_node in step_nodes.get(dep_step, []):
                for to_node in to_nodes:
                    rows.append({
                        "from": from_node,
                        "to": to_node,
                        "type": dep_type,
                        "carriers": carriers,
                        "relation": None,
                        "source": "fine_trace",
                        "from_step": dep_step,
                        "to_step": step_num,
                    })
            for start_id, start_steps in node_steps.items():
                if dep_step not in start_steps:
                    continue
                for end_id, end_steps in node_steps.items():
                    if step_num not in end_steps:
                        continue
                    rows.append({
                        "from": start_id,
                        "to": end_id,
                        "type": dep_type,
                        "carriers": carriers,
                        "relation": None,
                        "source": "fine_trace",
                        "from_step": dep_step,
                        "to_step": step_num,
                    })
            if not to_nodes:
                # Keep the fine-trace DAG traversable even when an intermediate
                # step is not materialized as an invariant node.
                intermediate_id = _fine_step_id(step_num)
                for from_node in step_nodes.get(dep_step, []):
                    rows.append({
                        "from": from_node,
                        "to": intermediate_id,
                        "type": dep_type,
                        "carriers": carriers,
                        "relation": None,
                        "source": "fine_trace",
                        "from_step": dep_step,
                        "to_step": step_num,
                    })
            if not step_nodes.get(dep_step):
                intermediate_id = _fine_step_id(dep_step)
                for to_node in to_nodes:
                    rows.append({
                        "from": intermediate_id,
                        "to": to_node,
                        "type": dep_type,
                        "carriers": carriers,
                        "relation": None,
                        "source": "fine_trace",
                        "from_step": dep_step,
                        "to_step": step_num,
                    })
            if not step_nodes.get(dep_step) and not to_nodes:
                rows.append({
                    "from": _fine_step_id(dep_step),
                    "to": _fine_step_id(step_num),
                    "type": dep_type,
                    "carriers": carriers,
                    "relation": None,
                    "source": "fine_trace",
                    "from_step": dep_step,
                    "to_step": step_num,
                    "from_var": dep_item.get("var"),
                    "to_var": item.get("var"),
                })
    return _dedupe_graph_edges(rows)


def _fine_trace_nodes(gt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fine_trace = gt.get("fine_trace")
    if not isinstance(fine_trace, list):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for item in fine_trace:
        if not isinstance(item, dict) or not isinstance(item.get("step"), int):
            continue
        node = dict(item)
        node["invariant_id"] = _fine_step_id(item["step"])
        node.setdefault("role", "intermediate")
        node["operands"] = _split_anchor_var(item.get("var"))
        rows[node["invariant_id"]] = node
    return rows


def _trace_steps_for_node(
    node: dict[str, Any],
    step_by_num: dict[int, dict[str, Any]],
    line_tolerance: int,
) -> list[int]:
    explicit = node.get("trace_step")
    if isinstance(explicit, int) and explicit in step_by_num:
        return [explicit]
    explicit = node.get("fine_trace_step")
    if isinstance(explicit, int) and explicit in step_by_num:
        return [explicit]
    return [
        step
        for step, item in step_by_num.items()
        if _location_matches(_loc(node), _loc(item), line_tolerance)
    ]


def _fine_step_id(step: int) -> str:
    return f"__gt_fine_step_{step}__"


def _dedupe_graph_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    seen: set[tuple[Any, ...]] = set()
    for edge in edges:
        key = (
            edge.get("from"),
            edge.get("to"),
            edge.get("type"),
            tuple(edge.get("carriers") or []),
            edge.get("source"),
            edge.get("from_step"),
            edge.get("to_step"),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(edge)
    return rows


def _graph_edges_from_agent(
    agent_edges: Any,
    node_by_id: dict[str, dict[str, Any]],
    line_tolerance: int,
) -> list[dict[str, Any]]:
    rows = []
    agents = agent_edges if isinstance(agent_edges, list) else []
    for index, edge in enumerate(agents):
        if not isinstance(edge, dict):
            continue
        from_ids = _matching_node_ids(edge.get("from"), node_by_id, line_tolerance)
        to_ids = _matching_node_ids(edge.get("to"), node_by_id, line_tolerance)
        for from_id in from_ids:
            for to_id in to_ids:
                rows.append({
                    "from": from_id,
                    "to": to_id,
                    "type": str(edge.get("type") or ""),
                    "carriers": edge.get("via") or [],
                    "relation": edge.get("relation"),
                    "agent_index": index,
                })
    return rows


def _matching_node_ids(
    point: Any,
    node_by_id: dict[str, dict[str, Any]],
    line_tolerance: int,
) -> list[str]:
    if not isinstance(point, dict):
        return []
    return [
        node_id
        for node_id, node in node_by_id.items()
        if _location_matches(_loc(node), _loc(point), line_tolerance)
    ]


def _edges_on_any_path(
    edges: list[dict[str, Any]],
    start_id: str,
    end_id: str,
) -> list[dict[str, Any]]:
    if not start_id or not end_id:
        return []
    forward = _reachable_from(edges, start_id)
    backward = _can_reach(edges, end_id)
    if end_id not in forward:
        return []
    return [
        edge
        for edge in edges
        if edge.get("from") in forward and edge.get("to") in backward
    ]


def _agent_edges_in_gt_segment(
    agent_edges: list[dict[str, Any]],
    gt_segment_edges: list[dict[str, Any]],
    start_id: str,
    end_id: str,
) -> list[dict[str, Any]]:
    """Return model edges that fall inside a GT semantic segment."""
    if not agent_edges:
        return []
    forward = _reachable_from(gt_segment_edges, start_id)
    backward = _can_reach(gt_segment_edges, end_id)
    segment_nodes = forward & backward
    if not segment_nodes:
        return []
    rows = []
    for edge in agent_edges:
        from_id = str(edge.get("from") or "")
        to_id = str(edge.get("to") or "")
        if from_id not in segment_nodes or to_id not in segment_nodes:
            continue
        if from_id == to_id or to_id in _reachable_from(gt_segment_edges, from_id):
            rows.append(edge)
    return rows


def _reachable_from(edges: list[dict[str, Any]], start_id: str) -> set[str]:
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(str(edge.get("from") or ""), []).append(str(edge.get("to") or ""))
    seen = {start_id}
    stack = [start_id]
    while stack:
        node = stack.pop()
        for target in adjacency.get(node, []):
            if target and target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


def _can_reach(edges: list[dict[str, Any]], end_id: str) -> set[str]:
    reverse: dict[str, list[str]] = {}
    for edge in edges:
        reverse.setdefault(str(edge.get("to") or ""), []).append(str(edge.get("from") or ""))
    seen = {end_id}
    stack = [end_id]
    while stack:
        node = stack.pop()
        for source in reverse.get(node, []):
            if source and source not in seen:
                seen.add(source)
                stack.append(source)
    return seen


def _edge_types(edges: list[dict[str, Any]]) -> set[str]:
    return {str(edge.get("type") or "") for edge in edges if str(edge.get("type") or "")}


def _edge_carriers(edges: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for edge in edges:
        for value in edge.get("carriers") or []:
            text = str(value).strip()
            if text and text not in seen:
                seen.add(text)
                values.append(text)
    return values


def _match_chain_relations(
    gt_edges: list[dict[str, Any]],
    agent_edges: list[dict[str, Any]],
    normalizer: ExpressionNormalizer,
) -> dict[str, Any]:
    gt_relations = [
        edge.get("relation")
        for edge in gt_edges
        if isinstance(edge.get("relation"), dict)
    ]
    agent_relations = [
        edge.get("relation")
        for edge in agent_edges
        if isinstance(edge.get("relation"), dict)
    ]
    if not gt_relations:
        return {"required": False, "matched": None}
    rows = []
    for gt_relation in gt_relations:
        comparisons = [
            _match_relation(
                gt_relation,
                agent_relation,
                normalizer,
                allow_flipped=False,
            )
            for agent_relation in agent_relations
        ]
        rows.append({
            "gt_relation": gt_relation,
            "matched": any(item.get("matched") is True for item in comparisons),
            "operand_partial_matched": any(
                item.get("operand_partial_matched") is True for item in comparisons
            ),
            "comparisons": comparisons,
        })
    return {
        "required": True,
        "matched": all(row["matched"] for row in rows),
        "operand_partial_matched": all(row["operand_partial_matched"] for row in rows),
        "details": rows,
    }


def _edge_summary(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": edge.get("from"),
        "to": edge.get("to"),
        "type": edge.get("type"),
        "carriers": edge.get("carriers") or [],
        "relation": edge.get("relation"),
        "invariant_id": edge.get("invariant_id"),
        "agent_index": edge.get("agent_index"),
    }


def _match_edge(
    gt_edge: dict[str, Any],
    node_by_id: dict[str, dict[str, Any]],
    agent_edges: Any,
    normalizer: ExpressionNormalizer,
    line_tolerance: int,
) -> dict[str, Any]:
    agents = agent_edges if isinstance(agent_edges, list) else []
    from_node = node_by_id.get(str(gt_edge.get("from_node") or ""))
    to_node = node_by_id.get(str(gt_edge.get("to_node") or ""))
    if from_node is None or to_node is None:
        return {
            "invariant_id": str(gt_edge.get("invariant_id") or ""),
            "available": False,
            "loc_hit": None,
            "exact_full_hit": None,
            "full_hit": None,
            "reason": "gt_edge_endpoint_missing",
        }
    loc_candidates = []
    exact_full_candidates = []
    full_candidates = []
    for index, agent in enumerate(agents):
        if not isinstance(agent, dict):
            continue
        from_match = _location_matches(_loc(from_node), _loc(agent.get("from")), line_tolerance)
        to_match = _location_matches(_loc(to_node), _loc(agent.get("to")), line_tolerance)
        loc_hit = bool(from_match and to_match)
        if not loc_hit:
            continue
        type_match = str(gt_edge.get("type") or "") == str(agent.get("type") or "")
        carrier_match = _match_all_operands(
            gt_edge.get("operands") or gt_edge.get("via") or [],
            agent.get("via") or [],
            normalizer,
        )
        relation_match = _match_relation(
            gt_edge.get("relation"),
            agent.get("relation"),
            normalizer,
            allow_flipped=False,
        ) if isinstance(gt_edge.get("relation"), dict) and isinstance(agent.get("relation"), dict) else None
        candidate = {
            "agent_index": index,
            "type_match": type_match,
            "carrier_match": carrier_match,
            "relation_match": relation_match,
        }
        loc_candidates.append(candidate)
        relation_required = isinstance(gt_edge.get("relation"), dict)
        relation_hit = (
            relation_match.get("matched") is True
            if isinstance(relation_match, dict)
            else not relation_required
        )
        if carrier_match.get("matched") and relation_hit:
            exact_full_candidates.append(candidate)
        if carrier_match.get("partial_matched") and relation_hit:
            full_candidates.append(candidate)
    best_loc = loc_candidates[0] if loc_candidates else None
    best_type = _best_candidate(loc_candidates, "type_match")
    best_carrier = _best_carrier_candidate(loc_candidates)
    best_carrier_partial = _best_carrier_candidate(loc_candidates, partial=True)
    best_exact_full = exact_full_candidates[0] if exact_full_candidates else None
    best_full = full_candidates[0] if full_candidates else None
    return {
        "invariant_id": str(gt_edge.get("invariant_id") or ""),
        "available": True,
        "loc_hit": bool(loc_candidates),
        "type_hit": bool(best_type),
        "carrier_hit": bool(best_carrier),
        "carrier_partial_hit": bool(best_carrier_partial),
        "exact_full_hit": bool(exact_full_candidates),
        "full_hit": bool(full_candidates),
        "best_loc": best_loc,
        "best_type": best_type,
        "best_carrier": best_carrier,
        "best_carrier_partial": best_carrier_partial,
        "best_exact_full": best_exact_full,
        "best_full": best_full,
        "gt": {
            "type": gt_edge.get("type"),
            "from": _node_summary(from_node),
            "to": _node_summary(to_node),
            "operands": gt_edge.get("operands") or gt_edge.get("via") or [],
            "relation": gt_edge.get("relation"),
        },
    }


def _match_all_operands(
    gt_operands: Any,
    agent_operands: Any,
    normalizer: ExpressionNormalizer,
) -> dict[str, Any]:
    gt_items = [str(item).strip() for item in (gt_operands or []) if str(item).strip()]
    agent_items = [str(item).strip() for item in (agent_operands or []) if str(item).strip()]
    rows = []
    for gt_operand in gt_items:
        comparisons = [
            normalizer.compare(gt_operand, agent_operand)
            for agent_operand in agent_items
        ]
        hard_match = _best_match(comparisons, allow_partial=False)
        partial_match = _best_match(comparisons, allow_partial=True)
        rows.append({
            "gt_operand": gt_operand,
            "matched": bool(hard_match and hard_match.get("matched")),
            "partial_matched": bool(partial_match and partial_match.get("matched")),
            "norm_tier": hard_match.get("norm_tier") if hard_match else "unresolved",
            "partial_norm_tier": partial_match.get("norm_tier") if partial_match else "unresolved",
            "agent_operand": hard_match.get("right") if hard_match else None,
            "partial_agent_operand": partial_match.get("right") if partial_match else None,
        })
    return {
        "matched": bool(gt_items) and all(row["matched"] for row in rows),
        "partial_matched": bool(gt_items) and all(row["partial_matched"] for row in rows),
        "matched_count": sum(row["matched"] for row in rows),
        "partial_matched_count": sum(row["partial_matched"] for row in rows),
        "total": len(rows),
        "gt_operands": gt_items,
        "agent_operands": agent_items,
        "details": rows,
    }


def _match_any_operand(
    gt_operands: Any,
    agent_operands: Any,
    normalizer: ExpressionNormalizer,
) -> dict[str, Any]:
    result = _match_all_operands(gt_operands, agent_operands, normalizer)
    total = int(result.get("total") or 0)
    matched_count = int(result.get("matched_count") or 0)
    partial_matched_count = int(result.get("partial_matched_count") or 0)
    result["match_policy"] = "any"
    result["matched"] = bool(total and matched_count > 0)
    result["partial_matched"] = bool(total and partial_matched_count > 0)
    result["recall"] = matched_count / total if total else None
    result["partial_recall"] = partial_matched_count / total if total else None
    return result


def _match_relation(
    gt_relation: Any,
    agent_relation: Any,
    normalizer: ExpressionNormalizer,
    *,
    allow_flipped: bool,
) -> dict[str, Any]:
    if not isinstance(gt_relation, dict):
        return {"required": True, "matched": False, "reason": "gt_relation_missing"}
    if _is_identity_relation(gt_relation, normalizer):
        return {
            "required": False,
            "matched": None,
            "mode": "gt_identity_relation_unscored",
            "reason": "gt_identity_relation_unscored",
            "gt_op": str(gt_relation.get("op") or ""),
            "agent_op": str((agent_relation or {}).get("op") or "")
            if isinstance(agent_relation, dict)
            else "",
        }
    if not isinstance(agent_relation, dict):
        return {"required": True, "matched": False, "reason": "agent_relation_missing"}
    gt_op = str(gt_relation.get("op") or "")
    agent_op = str(agent_relation.get("op") or "")
    exact_left = normalizer.compare(gt_relation.get("left"), agent_relation.get("left"))
    exact_right = normalizer.compare(gt_relation.get("right"), agent_relation.get("right"))
    exact_operands = bool(
        _is_hard_match(exact_left) and _is_hard_match(exact_right)
    )
    exact_partial_operands = bool(exact_left["matched"] and exact_right["matched"])
    exact = bool(gt_op == agent_op and exact_operands)
    flipped_left = flipped_right = {"matched": False, "norm_tier": "unresolved"}
    flipped = False
    flipped_partial_operands = False
    identity_equiv = False
    if allow_flipped and gt_op in FLIPPED_OPS:
        flipped_left = normalizer.compare(gt_relation.get("left"), agent_relation.get("right"))
        flipped_right = normalizer.compare(gt_relation.get("right"), agent_relation.get("left"))
        flipped_operands = bool(
            _is_hard_match(flipped_left) and _is_hard_match(flipped_right)
        )
        flipped_partial_operands = bool(flipped_left["matched"] and flipped_right["matched"])
        flipped = bool(
            FLIPPED_OPS[gt_op] == agent_op
            and flipped_operands
        )
    if gt_op in IDENTITY_OPS and agent_op in IDENTITY_OPS:
        identity_equiv = bool(exact_operands)
        if not identity_equiv:
            if flipped_left.get("norm_tier") == "unresolved":
                flipped_left = normalizer.compare(gt_relation.get("left"), agent_relation.get("right"))
                flipped_right = normalizer.compare(gt_relation.get("right"), agent_relation.get("left"))
                flipped_partial_operands = bool(flipped_left["matched"] and flipped_right["matched"])
            identity_equiv = bool(_is_hard_match(flipped_left) and _is_hard_match(flipped_right))
    operand_matched = exact_operands or (flipped if allow_flipped else False)
    operand_partial_matched = exact_partial_operands or flipped_partial_operands
    partial_equiv = bool(
        (gt_op == agent_op and exact_partial_operands)
        or (
            allow_flipped
            and gt_op in FLIPPED_OPS
            and FLIPPED_OPS[gt_op] == agent_op
            and flipped_partial_operands
        )
        or (
            gt_op in IDENTITY_OPS
            and agent_op in IDENTITY_OPS
            and operand_partial_matched
        )
    )
    return {
        "required": True,
        "matched": exact or flipped or identity_equiv or partial_equiv,
        "mode": (
            "exact"
            if exact
            else (
                "flipped"
                if flipped
                else (
                    "identity_equiv"
                    if identity_equiv
                    else ("partial_equiv" if partial_equiv else "miss")
                )
            )
        ),
        "op_match": gt_op == agent_op or identity_equiv,
        "operand_matched": operand_matched or identity_equiv,
        "operand_partial_matched": operand_partial_matched or identity_equiv,
        "partial_equiv_matched": partial_equiv,
        "gt_op": gt_op,
        "agent_op": agent_op,
        "exact": {
            "matched": exact,
            "left": exact_left,
            "right": exact_right,
        },
        "flipped": {
            "matched": flipped,
            "left": flipped_left,
            "right": flipped_right,
        } if allow_flipped else None,
    }


def _is_hard_match(row: dict[str, Any]) -> bool:
    return bool(row.get("matched") and row.get("norm_tier") != "partial")


def _best_match(rows: list[dict[str, Any]], *, allow_partial: bool = False) -> dict[str, Any] | None:
    matched = [
        row for row in rows
        if row.get("matched") and (allow_partial or row.get("norm_tier") != "partial")
    ]
    if not matched:
        return None
    priority = ExpressionNormalizer._TIER_PRIORITY
    return min(matched, key=lambda row: priority.get(str(row.get("norm_tier")), 99))


def _best_candidate(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get(key) is True:
            return row
    return None


def _best_carrier_candidate(rows: list[dict[str, Any]], *, partial: bool = False) -> dict[str, Any] | None:
    matched = [
        row for row in rows
        if isinstance(row.get("carrier_match"), dict)
        and row["carrier_match"].get("partial_matched" if partial else "matched") is True
    ]
    if not matched:
        return None
    priority = ExpressionNormalizer._TIER_PRIORITY

    def rank(row: dict[str, Any]) -> int:
        details = row.get("carrier_match", {}).get("details") or []
        if partial:
            tiers = [
                str(item.get("partial_norm_tier"))
                for item in details
                if item.get("partial_matched")
            ]
        else:
            tiers = [str(item.get("norm_tier")) for item in details if item.get("matched")]
        if not tiers:
            return 99
        return min(priority.get(tier, 99) for tier in tiers)

    return min(matched, key=rank)


def _is_identity_relation(relation: dict[str, Any], normalizer: ExpressionNormalizer) -> bool:
    if str(relation.get("op") or "") not in IDENTITY_OPS:
        return False
    left = relation.get("left")
    right = relation.get("right")
    comparison = normalizer.compare(left, right)
    return _is_hard_match(comparison)


def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "invariant_id": node.get("invariant_id"),
        "role": node.get("role"),
        "file": node.get("file"),
        "function": node.get("function"),
        "line": node.get("line"),
        "operands": node.get("operands") or [],
        "relation": node.get("relation"),
    }


def _point_summary(point: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": point.get("file"),
        "function": point.get("function"),
        "line": point.get("line"),
        "operands": point.get("operands") or [],
        "relation": point.get("relation"),
    }


def _norm_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"exact": 0, "structural": 0, "alias": 0, "constant": 0, "partial": 0, "unresolved": 0}

    def add_tier(value: Any) -> None:
        if isinstance(value, dict):
            tier = value.get("norm_tier")
            if tier in counts:
                counts[tier] += 1
            for nested in value.values():
                add_tier(nested)
        elif isinstance(value, list):
            for nested in value:
                add_tier(nested)

    for item in items:
        add_tier(item)
    return counts


def _norm_counts_for_matched_locations(
    points: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, int]:
    items: list[dict[str, Any]] = []
    for point in points:
        if point.get("location_match") is True:
            operand_match = point.get("operand_match")
            relation_match = point.get("relation_match")
            if isinstance(operand_match, dict):
                items.append(operand_match)
            if isinstance(relation_match, dict):
                items.append(relation_match)
    for edge in edges:
        if edge.get("loc_hit") is True:
            best_loc = edge.get("best_loc")
            if isinstance(best_loc, dict):
                items.append(best_loc)
    return _norm_counts(items)
