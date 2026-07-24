#!/usr/bin/env python3
"""Read a sample's verified GT and resolve its invariant operands to real
source names -- the small, self-contained set of helpers the reasoning scorer
needs. Kept independent of the GT-generation toolkit and of the (removed)
probing code so the evaluator has no upstream dependency.

Consumes the Stage-04 artifacts under gt_results/<sample_id>/:
  verified_assertions.json  -- assertions {id, kind, check:[op,left,right], at, from, invariants}
  verified_invariants.json  -- {edges:[{invariant_id, relation, from_line, to_line}], root_cause_criterion, nodes}
  field_bindings.json       -- {bindings: {"event.field": "<real source expression>"}}
  event_locations.json      -- {locations: {"event_id": {function, file, line}}}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# `$event.field` suffixes that resolve to a compile-time literal rather than a
# runtime-captured value (mirrors the GT toolkit's own literal set).
_LITERAL_FIELD_SUFFIXES = {
    "null_literal": "NULL",
    "zero_literal": "0",
    "true_literal": "true",
    "false_literal": "false",
}


def load(gt_dir: Path, name: str) -> dict[str, Any]:
    return json.loads((gt_dir / name).read_text(encoding="utf-8"))


def load_field_bindings(gt_dir: Path) -> dict[str, str]:
    path = gt_dir / "field_bindings.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("bindings", {}) if isinstance(data, dict) else {}


def load_event_locations(gt_dir: Path) -> dict[str, dict[str, Any]]:
    path = gt_dir / "event_locations.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("locations", {}) if isinstance(data, dict) else {}


def edge_for_assertion(assertion: dict[str, Any], edge_ids: set[str]) -> str | None:
    """The single edge invariant a transition assertion covers, or None when it
    covers zero or several (ambiguous)."""
    matches = [str(i) for i in assertion.get("invariants", []) if str(i) in edge_ids]
    return matches[0] if len(matches) == 1 else None


def operand_name(value: Any, assertion: dict[str, Any], edge: dict[str, Any],
                 field_bindings: dict[str, str]) -> str:
    """Human-facing name for one side of a transition check.

    Prefers the real vulnerable-source expression from field_bindings; then a
    compile-time literal; then a positional `value_at_L<line>` label from the
    edge's own endpoints; finally the raw semantic field name. Only the first
    two carry a real identity a subject's trace could match on -- the rest are
    internal labels that contribute no distinctive token, which is exactly the
    signal the scorer wants (an operand with no real name is not something the
    trace can be expected to mention)."""
    if not isinstance(value, str) or not value.startswith("$"):
        return json.dumps(value, ensure_ascii=False)
    stripped = value[1:]
    if stripped in field_bindings:
        return field_bindings[stripped]
    event_name, _, field = stripped.rpartition(".")
    if field in _LITERAL_FIELD_SUFFIXES:
        return _LITERAL_FIELD_SUFFIXES[field]
    if event_name == assertion.get("from") and edge.get("from_line"):
        return f"value_at_L{edge['from_line']}"
    if event_name == assertion.get("at") and edge.get("to_line"):
        return f"value_at_L{edge['to_line']}"
    return stripped
