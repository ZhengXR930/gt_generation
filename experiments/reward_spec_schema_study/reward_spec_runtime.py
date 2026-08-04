"""Compile and evaluate minimal issue-guided Reward Specs without an LLM judge."""

from __future__ import annotations

import ast
import itertools
import re
from typing import Any


DIMENSIONS = ("admission", "root", "target")
OBSERVABILITY = {"direct", "derived", "proxy", "unresolved"}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_REFERENCE = re.compile(
    r"\b(?:root\.)?[a-z][a-z0-9_]*\.(?:hit|time|[a-z][a-z0-9_]*)\b"
)
_CALL = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(")
_ASSIGN = re.compile(r"(?<![=!<>])=(?!=)")


class RewardSpecError(ValueError):
    pass


def _parse_anchor(anchor: str) -> tuple[str, str, int]:
    try:
        file_name, function, line_text = anchor.rsplit(":", 2)
        line = int(line_text)
    except (AttributeError, ValueError) as exc:
        raise RewardSpecError(f"invalid source anchor: {anchor!r}") from exc
    if not file_name or not function or line <= 0:
        raise RewardSpecError(f"invalid source anchor: {anchor!r}")
    return file_name, function, line


def _validate_capture(expression: str) -> None:
    if not expression.strip():
        raise RewardSpecError("capture expression is empty")
    if _CALL.search(expression):
        raise RewardSpecError(f"function-call capture is forbidden: {expression}")
    if _ASSIGN.search(expression) or ";" in expression or "++" in expression or "--" in expression:
        raise RewardSpecError(f"side-effecting capture is forbidden: {expression}")


def validate_spec(spec: dict[str, Any]) -> None:
    if spec.get("version") != "reward-spec-v1":
        raise RewardSpecError("unsupported Reward Spec version")
    for dimension in DIMENSIONS:
        contract = spec.get(dimension)
        if not isinstance(contract, dict):
            raise RewardSpecError(f"missing {dimension} contract")
        if contract.get("observability") not in OBSERVABILITY:
            raise RewardSpecError(f"invalid {dimension} observability")
        events = contract.get("events")
        predicate = contract.get("predicate")
        if not isinstance(events, list) or len(events) > 3 or not isinstance(predicate, str):
            raise RewardSpecError(f"invalid {dimension} verifier")
        if contract["observability"] == "unresolved" and (events or predicate):
            raise RewardSpecError(f"unresolved {dimension} must have an empty verifier")
        seen: set[str] = set()
        for event in events:
            event_id = str(event.get("id") or "")
            if not _IDENTIFIER.fullmatch(event_id) or event_id in seen:
                raise RewardSpecError(f"invalid/duplicate {dimension} event id: {event_id}")
            seen.add(event_id)
            _parse_anchor(event.get("at"))
            captures = event.get("capture")
            if not isinstance(captures, list):
                raise RewardSpecError(f"invalid captures for {dimension}.{event_id}")
            capture_names: set[str] = set()
            for capture in captures:
                name = str(capture.get("name") or "")
                expression = str(capture.get("expression") or "")
                if not _IDENTIFIER.fullmatch(name) or name in capture_names:
                    raise RewardSpecError(f"invalid/duplicate capture: {dimension}.{event_id}.{name}")
                capture_names.add(name)
                _validate_capture(expression)


def compile_checkpoints(spec: dict[str, Any]) -> list[dict[str, Any]]:
    validate_spec(spec)
    checkpoints: list[dict[str, Any]] = []
    for dimension in DIMENSIONS:
        for order, event in enumerate(spec[dimension]["events"]):
            file_name, function, line = _parse_anchor(event["at"])
            checkpoints.append(
                {
                    "kind": "condition_event",
                    "event_point": f"{dimension}.{event['id']}",
                    "assertion_role": [dimension],
                    "expected_order": order,
                    "file": file_name,
                    "function": function,
                    "line": line,
                    "captures": {
                        capture["name"]: capture["expression"]
                        for capture in event["capture"]
                    },
                }
            )
    return checkpoints


_ALLOWED_AST = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Invert,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.LShift,
    ast.RShift,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Name,
    ast.Load,
    ast.Constant,
)


def _python_expression(predicate: str, values: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    expression = predicate.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    expression = re.sub(r"\bNULL\b", "0", expression)
    expression = re.sub(r"\btrue\b", "True", expression, flags=re.I)
    expression = re.sub(r"\bfalse\b", "False", expression, flags=re.I)
    locals_: dict[str, Any] = {}

    def replace(match: re.Match[str]) -> str:
        reference = match.group(0)
        if reference not in values:
            raise RewardSpecError(f"predicate reference was not captured: {reference}")
        name = f"value_{len(locals_)}"
        locals_[name] = values[reference]
        return name

    expression = _REFERENCE.sub(replace, expression).strip()
    return expression, locals_


def _evaluate_predicate(predicate: str, values: dict[str, Any]) -> bool:
    expression, locals_ = _python_expression(predicate, values)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise RewardSpecError(f"invalid predicate syntax: {predicate}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST):
            raise RewardSpecError(f"forbidden predicate operation: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in locals_:
            raise RewardSpecError(f"unknown predicate name: {node.id}")
    try:
        return bool(eval(compile(tree, "<reward-predicate>", "eval"), {"__builtins__": {}}, locals_))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise RewardSpecError(f"predicate evaluation failed: {exc}") from exc


def _hit_values(prefix: str, event_id: str, hit: dict[str, Any]) -> dict[str, Any]:
    base = f"{prefix}{event_id}"
    values = {
        f"{base}.hit": True,
        f"{base}.time": hit.get("timestamp"),
    }
    for name, value in (hit.get("fields") or {}).items():
        values[f"{base}.{name}"] = value
    return values


def _event_hits(hits: list[dict[str, Any]], dimension: str, event_id: str) -> list[dict[str, Any]]:
    point = f"{dimension}.{event_id}"
    return [
        hit for hit in hits
        if hit.get("event_point") == point and not hit.get("breakpoint_error")
    ]


def _contract_witnesses(
    spec: dict[str, Any],
    dimension: str,
    hits: list[dict[str, Any]],
    root_witnesses: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    contract = spec[dimension]
    if contract["observability"] == "unresolved":
        return [], None
    events = contract["events"]
    if not events or not contract["predicate"]:
        return [], "empty executable verifier"
    choices = [_event_hits(hits, dimension, event["id"]) for event in events]
    if any(not choice for choice in choices):
        return [], None
    roots = root_witnesses if dimension == "target" else [None]
    if dimension == "target" and not roots:
        return [], None
    witnesses: list[dict[str, Any]] = []
    error: str | None = None
    for root_witness in roots:
        for selected in itertools.product(*choices):
            values: dict[str, Any] = {}
            evidence: dict[str, Any] = {}
            for event, hit in zip(events, selected):
                event_id = event["id"]
                values.update(_hit_values("", event_id, hit))
                # Accept `root.<id>.*` in Root itself; several generated specs
                # use the explicit namespace even before crossing dimensions.
                if dimension == "root":
                    values.update(_hit_values("root.", event_id, hit))
                evidence[event_id] = {
                    "timestamp": hit.get("timestamp"),
                    "file": hit.get("file"),
                    "function": hit.get("function"),
                    "line": hit.get("line"),
                    "fields": hit.get("fields") or {},
                }
            if root_witness:
                values.update(root_witness["values"])
            try:
                if _evaluate_predicate(contract["predicate"], values):
                    witness_values = dict(values)
                    if dimension == "root":
                        # Export only the stable cross-dimension namespace.
                        witness_values = {
                            key: value for key, value in values.items()
                            if key.startswith("root.")
                        }
                    witnesses.append({"values": witness_values, "evidence": evidence})
            except RewardSpecError as exc:
                error = str(exc)
                break
        if error:
            break
    return witnesses, error


def summarize_reward(spec: dict[str, Any], hits: list[dict[str, Any]], exit_code: int | None) -> dict[str, Any]:
    validate_spec(spec)
    results: dict[str, Any] = {}
    root_witnesses: list[dict[str, Any]] = []
    for dimension in DIMENSIONS:
        contract = spec[dimension]
        witnesses, error = _contract_witnesses(
            spec,
            dimension,
            hits,
            root_witnesses=root_witnesses,
        )
        if dimension == "root":
            root_witnesses = witnesses
        event_counts = {
            event["id"]: len(_event_hits(hits, dimension, event["id"]))
            for event in contract["events"]
        }
        all_reached = bool(event_counts) and all(event_counts.values())
        if contract["observability"] == "unresolved":
            status = "unresolved"
        elif error:
            status = "invalid_verifier"
        elif witnesses:
            status = (
                "proxy_observed"
                if contract["observability"] == "proxy"
                else "satisfied"
            )
        elif all_reached:
            status = "not_satisfied"
        else:
            status = "not_reached"
        results[dimension] = {
            "goal": contract["goal"],
            "observability": contract["observability"],
            "status": status,
            "event_hits": event_counts,
            "witness": witnesses[0]["evidence"] if witnesses else None,
            "error": error,
        }

    stage = 0
    for index, dimension in enumerate(DIMENSIONS, 1):
        if results[dimension]["status"] != "satisfied":
            break
        stage = index
    target_triggered = exit_code not in (None, 0, 300)
    return {
        "source": "frozen_issue_codebase_reward_spec_v1",
        "uses_hidden_gt": False,
        "reward": results,
        "verified_stage": stage,
        "target_runtime": {
            "exit_code": exit_code,
            "triggered": target_triggered,
        },
    }
