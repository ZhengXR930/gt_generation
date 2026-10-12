#!/usr/bin/env python3
"""Deterministic vulnerability-logic scoring against verified invariants.

This evaluator compares ``analysis.json``'s embedded ``vuln_logic`` object to
the GT source/root-cause/sink anchors and verified propagation nodes.  It does
not read free-form explanations and does not call an LLM judge.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from evaluator.field_bindings import parse_binding


REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"

OPS = {"eq", "ne", "lt", "le", "gt", "ge", "same_object"}
FLIPPED_OPS = {"lt": "gt", "gt": "lt", "le": "ge", "ge": "le", "eq": "eq", "ne": "ne"}
IDENTITY_OPS = {"eq", "same_object"}
EDGE_TYPES = {"data", "control", "order"}
PROPAGATION_NODE_ROLES = {
    "source",
    "intermediate",
    "root_cause",
    "root_obligation",
    "obligation",
    "sink",
}
DEFAULT_LINE_TOLERANCE = 5

_SPACE_RE = re.compile(r"\s+")
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
    source_anchor = _gt_point_anchor(gt.get("source"), "source")
    root_cause_anchor = _gt_point_anchor(gt.get("root_cause"), "root_cause")
    sink_anchor = _gt_point_anchor(gt.get("sink"), "sink")

    source_result = _match_point(
        source_anchor,
        logic.get("source"),
        normalizer,
        line_tolerance=line_tolerance,
        require_relation=False,
        allow_flipped_relation=False,
    )
    root_cause_result = _match_point(
        root_cause_anchor,
        logic.get("root_cause"),
        normalizer,
        line_tolerance=line_tolerance,
        require_relation=True,
        allow_flipped_relation=True,
    )
    sink_result = _match_point(
        sink_anchor,
        logic.get("sink"),
        normalizer,
        line_tolerance=line_tolerance,
        require_relation=True,
        allow_flipped_relation=True,
    )
    propagation_node_scores = _score_propagation_nodes(
        nodes,
        edges,
        logic.get("propagation"),
        line_tolerance,
    )
    matched_location_norm_counts = _norm_counts_for_matched_locations(
        [source_result, root_cause_result, sink_result]
    )
    all_norm_counts = _norm_counts([source_result, root_cause_result, sink_result])
    source_scores = _point_scores(source_result, require_relation=False)
    root_cause_scores = _point_scores(root_cause_result, require_relation=True)
    sink_scores = _point_scores(sink_result, require_relation=True)
    propagation_scores = {
        "mode": "node_partial_order",
        "node_recall": propagation_node_scores["node_recall"],
        "node_precision": propagation_node_scores["node_precision"],
        "ordered_coverage": propagation_node_scores["ordered_coverage"],
        "gt_nodes_total": propagation_node_scores["gt_nodes_total"],
        "agent_nodes_total": propagation_node_scores["agent_nodes_total"],
        "matched_nodes": propagation_node_scores["matched_nodes"],
        "ordered_nodes": propagation_node_scores["ordered_nodes"],
    }
    return {
        "evaluation_protocol": "vuln-logic-invariant-reasoning-v3",
        "sample_id": sample_id,
        "line_tolerance": line_tolerance,
        "dimension_scores": {
            "source": source_scores,
            "root_cause": root_cause_scores,
            "sink": sink_scores,
            "propagation": propagation_scores,
        },
        "source_location_score": source_scores["location"],
        "source_operand_score": source_scores["operand"],
        "root_cause_location_score": root_cause_scores["location"],
        "root_cause_operand_score": root_cause_scores["operand"],
        "root_cause_relation_score": root_cause_scores["relation"],
        "sink_location_score": sink_scores["location"],
        "sink_operand_score": sink_scores["operand"],
        "sink_relation_score": sink_scores["relation"],
        "propagation_node_recall_score": propagation_node_scores["node_recall"],
        "propagation_node_precision_score": propagation_node_scores["node_precision"],
        "propagation_ordered_coverage_score": propagation_node_scores["ordered_coverage"],
        "propagation_gt_node_total": propagation_node_scores["gt_nodes_total"],
        "propagation_agent_node_total": propagation_node_scores["agent_nodes_total"],
        "propagation_node_matches": propagation_node_scores["matched_nodes"],
        "propagation_ordered_matches": propagation_node_scores["ordered_nodes"],
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
            "root_cause": root_cause_result,
            "sink": sink_result,
            "propagation_nodes": propagation_node_scores["diagnostics"],
        },
    }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _bool_score(value: Any, *, available: bool = True) -> int | None:
    if not available:
        return None
    return int(value is True)


def _point_scores(
    result: dict[str, Any],
    *,
    require_relation: bool,
) -> dict[str, int | None]:
    available = result.get("available") is not False
    relation = result.get("relation_match") if isinstance(result.get("relation_match"), dict) else {}
    operand = result.get("operand_match") if isinstance(result.get("operand_match"), dict) else {}
    operand_hit = bool(
        result.get("location_match") is True
        and operand.get("matched") is True
    )
    relation_required = relation.get("required") is not False
    relation_hit = bool(
        operand_hit
        and (not relation_required or relation.get("matched") is True)
    )
    scores = {
        "location": _bool_score(result.get("location_match"), available=available),
        "operand": _bool_score(operand_hit, available=available),
    }
    if require_relation:
        scores["relation"] = _bool_score(relation_hit, available=available)
    return scores


def _score_propagation_nodes(
    gt_nodes: list[dict[str, Any]],
    gt_edges: list[dict[str, Any]],
    agent_edges: Any,
    line_tolerance: int,
) -> dict[str, Any]:
    sequence = _propagation_gt_node_sequence(gt_nodes)
    agent_points = _agent_propagation_points(agent_edges)
    matched_count, matched_gt, matched_agent = _maximum_location_matching(
        sequence,
        agent_points,
        line_tolerance,
    )
    ordered_count = _ordered_location_coverage(
        sequence,
        gt_edges,
        agent_points,
        line_tolerance,
    )
    gt_total = len(sequence)
    agent_total = len(agent_points)
    recall = (matched_count / gt_total) if gt_total else None
    precision = (
        (matched_count / agent_total)
        if gt_total and agent_total
        else (0.0 if gt_total else None)
    )
    ordered_coverage = (ordered_count / gt_total) if gt_total else None
    return {
        "node_recall": recall,
        "node_precision": precision,
        "ordered_coverage": ordered_coverage,
        "gt_nodes_total": gt_total,
        "agent_nodes_total": agent_total,
        "matched_nodes": matched_count,
        "ordered_nodes": ordered_count,
        "diagnostics": {
            "mode": "node_partial_order",
            "line_tolerance": line_tolerance,
            "gt_nodes": [
                {
                    "index": index,
                    "matched": index in matched_gt,
                    **_node_summary(node),
                }
                for index, node in enumerate(sequence)
            ],
            "agent_nodes": [
                {
                    "index": index,
                    "matched": index in matched_agent,
                    **_point_summary(point),
                }
                for index, point in enumerate(agent_points)
            ],
        },
    }


def _propagation_gt_node_sequence(
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = [
        node
        for node in nodes
        if isinstance(node, dict)
        and str(node.get("role") or "") in PROPAGATION_NODE_ROLES
        and _has_location(node)
    ]
    ordered = [node for node in candidates if _node_order(node) is not None]
    if ordered:
        candidates = ordered
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda item: _node_sequence_sort_key(item[1], item[0]))
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for _, node in indexed:
        key = _node_dedupe_key(node)
        if key in seen:
            continue
        seen.add(key)
        rows.append(node)
    return rows


def _agent_propagation_points(agent_edges: Any) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    agents = agent_edges if isinstance(agent_edges, list) else []
    for edge in agents:
        if not isinstance(edge, dict):
            continue
        for endpoint in ("from", "to"):
            point = edge.get(endpoint)
            if not isinstance(point, dict) or not _has_location(point):
                continue
            key = _point_dedupe_key(point)
            if key in seen:
                continue
            seen.add(key)
            points.append(point)
    return points


def _maximum_location_matching(
    gt_nodes: list[dict[str, Any]],
    agent_points: list[dict[str, Any]],
    line_tolerance: int,
) -> tuple[int, set[int], set[int]]:
    gt_to_agent: dict[int, int] = {}

    def augment(agent_index: int, seen: set[int]) -> bool:
        for gt_index, node in enumerate(gt_nodes):
            if gt_index in seen:
                continue
            if not _location_matches(_loc(node), _loc(agent_points[agent_index]), line_tolerance):
                continue
            seen.add(gt_index)
            if gt_index not in gt_to_agent or augment(gt_to_agent[gt_index], seen):
                gt_to_agent[gt_index] = agent_index
                return True
        return False

    for agent_index in range(len(agent_points)):
        augment(agent_index, set())
    matched_gt = set(gt_to_agent)
    matched_agent = set(gt_to_agent.values())
    return len(matched_gt), matched_gt, matched_agent


def _ordered_location_coverage(
    gt_nodes: list[dict[str, Any]],
    gt_edges: list[dict[str, Any]],
    agent_points: list[dict[str, Any]],
    line_tolerance: int,
) -> int:
    if not gt_nodes or not agent_points:
        return 0
    reachable_after = _propagation_reachability_masks(gt_nodes, gt_edges)
    point_matches = [
        _matching_gt_mask(gt_nodes, point, line_tolerance)
        for point in agent_points
    ]

    @lru_cache(maxsize=None)
    def best_from(agent_index: int, selected_gt: int) -> int:
        if agent_index >= len(point_matches):
            return 0
        best = best_from(agent_index + 1, selected_gt)
        candidates = point_matches[agent_index] & ~selected_gt
        while candidates:
            bit = candidates & -candidates
            gt_index = bit.bit_length() - 1
            candidates ^= bit
            # Agent order says every previously selected GT node is before this
            # candidate. That only violates the GT partial order when the
            # candidate can reach one of those previous nodes in the GT graph.
            if reachable_after[gt_index] & selected_gt:
                continue
            best = max(best, 1 + best_from(agent_index + 1, selected_gt | bit))
        return best

    return best_from(0, 0)


def _matching_gt_mask(
    gt_nodes: list[dict[str, Any]],
    agent_point: dict[str, Any],
    line_tolerance: int,
) -> int:
    mask = 0
    for index, node in enumerate(gt_nodes):
        if _location_matches(_loc(node), _loc(agent_point), line_tolerance):
            mask |= 1 << index
    return mask


def _propagation_reachability_masks(
    gt_nodes: list[dict[str, Any]],
    gt_edges: list[dict[str, Any]],
) -> list[int]:
    node_count = len(gt_nodes)
    masks = [0] * node_count
    node_id_to_index = {
        str(node.get("invariant_id") or ""): index
        for index, node in enumerate(gt_nodes)
        if str(node.get("invariant_id") or "")
    }
    for edge in gt_edges:
        from_index = node_id_to_index.get(str(edge.get("from_node") or ""))
        to_index = node_id_to_index.get(str(edge.get("to_node") or ""))
        if from_index is None or to_index is None:
            from_index = _edge_endpoint_index(
                gt_nodes,
                edge,
                prefix="from",
            )
            to_index = _edge_endpoint_index(gt_nodes, edge, prefix="to")
        if from_index is None or to_index is None or from_index == to_index:
            continue
        masks[from_index] |= 1 << to_index

    changed = True
    while changed:
        changed = False
        for index in range(node_count):
            expanded = masks[index]
            targets = expanded
            while targets:
                bit = targets & -targets
                target_index = bit.bit_length() - 1
                targets ^= bit
                expanded |= masks[target_index]
            if expanded != masks[index]:
                masks[index] = expanded
                changed = True
    return masks


def _edge_endpoint_index(
    gt_nodes: list[dict[str, Any]],
    edge: dict[str, Any],
    *,
    prefix: str,
) -> int | None:
    endpoint = {
        "file": edge.get(f"{prefix}_file"),
        "function": edge.get(f"{prefix}_function"),
        "line": edge.get(f"{prefix}_line"),
    }
    if not _has_location(endpoint):
        location = edge.get(f"{prefix}_location")
        endpoint = location if isinstance(location, dict) else endpoint
    for index, node in enumerate(gt_nodes):
        if _point_dedupe_key(node) == _point_dedupe_key(endpoint):
            return index
    return None


def _has_location(item: dict[str, Any]) -> bool:
    return bool(item.get("file") and item.get("function") and item.get("line") is not None)


def _node_order(node: dict[str, Any]) -> int | None:
    for key in ("fine_trace_step", "trace_step", "step"):
        value = _int_or_none(node.get(key))
        if value is not None:
            return value
    return None


def _node_sequence_sort_key(node: dict[str, Any], index: int) -> tuple[Any, ...]:
    order = _node_order(node)
    if order is not None:
        return (0, order, _int_or_none(node.get("line")) or 0, index)
    return (
        1,
        _norm_path(node.get("file")),
        _norm_function(node.get("function")),
        _int_or_none(node.get("line")) or 0,
        index,
    )


def _node_dedupe_key(node: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _node_order(node),
        _norm_path(node.get("file")),
        _norm_function(node.get("function")),
        _int_or_none(node.get("line")),
    )


def _point_dedupe_key(point: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _norm_path(point.get("file")),
        _norm_function(point.get("function")),
        _int_or_none(point.get("line")),
    )


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


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


def _function_matches(left: Any, right: Any) -> bool:
    left_norm = _norm_function(left)
    right_norm = _norm_function(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    if "::" in left_norm and "::" in right_norm:
        return False
    qualified, plain = (
        (left_norm, right_norm)
        if "::" in left_norm
        else (right_norm, left_norm)
    )
    return qualified.split("::")[-1] == plain


def _location_matches(gt: Location, agent: Location, tolerance: int) -> bool:
    if gt.line is None or agent.line is None:
        line_match = gt.line is None and agent.line is None
    else:
        line_match = abs(gt.line - agent.line) <= tolerance
    return (
        _file_matches(gt.file, agent.file)
        and _function_matches(gt.function, agent.function)
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
        return {"available": False, "hit": None, "reason": "gt_anchor_missing"}
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
    operand_hit = bool(loc_match and operand_match.get("matched"))
    relation_hit = bool(
        operand_hit
        and (not relation_required or relation_match.get("matched") is True)
    )
    exact_hit = bool(
        loc_match
        and operand_match.get("matched")
        and (not relation_required or relation_match.get("matched") is True)
    )
    return {
        "available": True,
        "hit": relation_hit,
        "exact_hit": exact_hit,
        "location_match": loc_match,
        "operand_match": operand_match,
        "relation_match": relation_match,
        "gt": _node_summary(gt_node),
        "agent": _point_summary(agent_point),
    }


def _gt_point_anchor(
    anchor: Any,
    role: str,
) -> dict[str, Any] | None:
    if not isinstance(anchor, dict):
        return None
    anchor_operands = anchor.get("operands") or _split_anchor_var(anchor.get("var"))
    return {
        "invariant_id": _gt_anchor_id(role),
        "role": role,
        "file": anchor.get("file"),
        "function": anchor.get("function"),
        "line": anchor.get("line"),
        "line_end": anchor.get("line_end"),
        "operands": anchor_operands,
        "relation": anchor.get("relation"),
        "trace_step": anchor.get("trace_step"),
        "fine_trace_step": anchor.get("trace_step"),
        "code": anchor.get("code"),
    }


def _gt_anchor_id(role: str) -> str:
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
        hard_match = _best_match(comparisons)
        rows.append({
            "gt_operand": gt_operand,
            "matched": bool(hard_match and hard_match.get("matched")),
            "norm_tier": hard_match.get("norm_tier") if hard_match else "unresolved",
            "agent_operand": hard_match.get("right") if hard_match else None,
        })
    return {
        "matched": bool(gt_items) and all(row["matched"] for row in rows),
        "matched_count": sum(row["matched"] for row in rows),
        "total": len(rows),
        "gt_operands": gt_items,
        "agent_operands": agent_items,
        "details": rows,
    }


def _match_relation(
    gt_relation: Any,
    agent_relation: Any,
    normalizer: ExpressionNormalizer,
    *,
    allow_flipped: bool,
) -> dict[str, Any]:
    if not isinstance(gt_relation, dict):
        return {"required": True, "matched": False, "reason": "gt_relation_missing"}
    if not isinstance(agent_relation, dict):
        return {"required": True, "matched": False, "reason": "agent_relation_missing"}
    gt_op = str(gt_relation.get("op") or "")
    agent_op = str(agent_relation.get("op") or "")
    exact_left = normalizer.compare(gt_relation.get("left"), agent_relation.get("left"))
    exact_right = normalizer.compare(gt_relation.get("right"), agent_relation.get("right"))
    exact_operands = bool(
        _is_hard_match(exact_left) and _is_hard_match(exact_right)
    )
    exact = bool(gt_op == agent_op and exact_operands)
    flipped_left = flipped_right = {"matched": False, "norm_tier": "unresolved"}
    flipped = False
    identity_equiv = False
    if allow_flipped and gt_op in FLIPPED_OPS:
        flipped_left = normalizer.compare(gt_relation.get("left"), agent_relation.get("right"))
        flipped_right = normalizer.compare(gt_relation.get("right"), agent_relation.get("left"))
        flipped_operands = bool(
            _is_hard_match(flipped_left) and _is_hard_match(flipped_right)
        )
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
            identity_equiv = bool(_is_hard_match(flipped_left) and _is_hard_match(flipped_right))
    operand_matched = exact_operands or (flipped if allow_flipped else False)
    hard_matched = exact or flipped or identity_equiv
    return {
        "required": True,
        "matched": hard_matched,
        "mode": (
            "exact"
            if exact
            else (
                "flipped"
                if flipped
                else (
                    "identity_equiv" if identity_equiv else "miss"
                )
            )
        ),
        "op_match": gt_op == agent_op or identity_equiv,
        "operand_matched": operand_matched or identity_equiv,
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
    return bool(row.get("matched"))


def _best_match(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    matched = [row for row in rows if row.get("matched")]
    if not matched:
        return None
    priority = ExpressionNormalizer._TIER_PRIORITY
    return min(matched, key=lambda row: priority.get(str(row.get("norm_tier")), 99))


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
    counts = {"exact": 0, "structural": 0, "alias": 0, "constant": 0, "unresolved": 0}

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


def _norm_counts_for_matched_locations(points: list[dict[str, Any]]) -> dict[str, int]:
    items: list[dict[str, Any]] = []
    for point in points:
        if point.get("location_match") is True:
            operand_match = point.get("operand_match")
            relation_match = point.get("relation_match")
            if isinstance(operand_match, dict):
                items.append(operand_match)
            if isinstance(relation_match, dict):
                items.append(relation_match)
    return _norm_counts(items)
