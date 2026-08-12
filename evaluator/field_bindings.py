"""Helpers for GT field binding expressions and scoped aliases.

Stage 04 historically wrote field_bindings.json as:

    {"bindings": {"event.field": "source_expr"}}

The alias-compatible form keeps that contract but also allows a binding value
to be an object:

    {
      "expr": "source_expr",
      "canonical": "event.field",
      "aliases": ["source_expr", "MACRO(source_expr)"]
    }

The old string form is interpreted as expr=value, canonical=key, aliases=[value].
Aliases are intentionally scoped by the binding key; callers should first match
the event/location and only then resolve operands through that event's bindings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldBinding:
    key: str
    expr: str
    canonical: str
    aliases: tuple[str, ...]


_SPACE_RE = re.compile(r"\s+")


def normalize_expr(value: Any) -> str:
    """Normalize a source expression for deterministic alias lookup."""
    text = str(value or "").strip()
    return _SPACE_RE.sub("", text)


def parse_binding(key: str, value: Any) -> FieldBinding:
    if isinstance(value, dict):
        expr = str(value.get("expr") or value.get("source") or "").strip()
        canonical = str(value.get("canonical") or key).strip() or key
        raw_aliases = value.get("aliases")
        aliases = []
        if isinstance(raw_aliases, list):
            aliases = [str(item).strip() for item in raw_aliases if str(item).strip()]
        if expr and expr not in aliases:
            aliases.insert(0, expr)
        return FieldBinding(
            key=key,
            expr=expr,
            canonical=canonical,
            aliases=tuple(aliases),
        )
    expr = str(value or "").strip()
    return FieldBinding(key=key, expr=expr, canonical=key, aliases=(expr,) if expr else ())


def binding_expr(bindings: dict[str, Any], key: str, default: str = "") -> str:
    """Return the source expression for a v1 or v2 field binding."""
    if key not in bindings:
        return default
    return parse_binding(key, bindings[key]).expr


def alias_index_for_event(
    bindings: dict[str, Any],
    event: str | None,
) -> dict[str, str]:
    """Build normalized expression/alias -> canonical id for one event scope."""
    index: dict[str, str] = {}
    prefix = f"{event}." if event else ""
    for key, value in bindings.items():
        key_text = str(key)
        if prefix and not key_text.startswith(prefix):
            continue
        binding = parse_binding(key_text, value)
        candidates = list(binding.aliases)
        if binding.expr:
            candidates.append(binding.expr)
        candidates.append(binding.canonical)
        candidates.append(binding.key)
        for candidate in candidates:
            normalized = normalize_expr(candidate)
            if normalized:
                index[normalized] = binding.canonical
    return index


def canonical_for_operand(
    expression: Any,
    bindings: dict[str, Any],
    event: str | None,
) -> str | None:
    """Resolve an agent operand expression to a scoped canonical GT operand."""
    normalized = normalize_expr(expression)
    if not normalized:
        return None
    return alias_index_for_event(bindings, event).get(normalized)
