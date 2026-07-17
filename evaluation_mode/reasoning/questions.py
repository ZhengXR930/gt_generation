"""Select and render three tool-free probes from immutable verified GT."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any


_SYMBOLS = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
_HINT_TERMS = (
    "lower-bound",
    "lower bound",
    "upper-bound",
    "upper bound",
    "oversized",
    "exceeds",
    "short read",
    "corrupted",
    "changed",
)
_COMPARISON_RE = re.compile(
    r"(-?\d+|[A-Za-z_][A-Za-z0-9_.>:-]*)\s*(<=|>=|==|!=|<|>)\s*"
    r"(-?\d+|[A-Za-z_][A-Za-z0-9_.>:-]*)"
)
_REVERSE_OP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}
_COMPLEMENT_OP = {"<": ">=", "<=": ">", ">": "<=", ">=": "<", "==": "!=", "!=": "=="}


def _format_operand(value: Any) -> str:
    if isinstance(value, str) and value.startswith("$"):
        return value[1:]
    return json.dumps(value, ensure_ascii=False)


def format_check(check: list[Any]) -> str:
    op_name, left, right = check
    return f"{_format_operand(left)} {_SYMBOLS[op_name]} {_format_operand(right)}"


def public_context_text(sample_info: dict[str, Any], crash_trace: str) -> str:
    issue = sample_info.get("original_bug_description")
    if not issue and isinstance(sample_info.get("issue_description"), dict):
        issue = sample_info["issue_description"].get("original")
    return "\n".join(part for part in (str(issue or "").strip(), crash_trace.strip()) if part)


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9_<>=!&|.+*\-]", "", value.lower())


def _comparison_atoms(value: str) -> list[tuple[str, str, str]]:
    atoms: list[tuple[str, str, str]] = []
    for left, op, right in _COMPARISON_RE.findall(value):
        if left.lstrip("-").isdigit() and not right.lstrip("-").isdigit():
            left, right, op = right, left, _REVERSE_OP[op]
        atoms.append((_compact(left), op, _compact(right)))
    return atoms


def explicitly_leaked(answer: str, public_text: str) -> bool:
    """Detect only explicit answer disclosure; inference remains part of the task."""
    compact_answer = _compact(answer)
    compact_public = _compact(public_text)
    if bool(
        compact_answer
        and len(compact_answer) >= 6
        and compact_answer in compact_public
    ):
        return True
    answer_atoms = _comparison_atoms(answer)
    public_atoms = _comparison_atoms(public_text)
    return any(
        left == public_left
        and right == public_right
        and public_op in {op, _COMPLEMENT_OP[op]}
        for left, op, right in answer_atoms
        for public_left, public_op, public_right in public_atoms
    )


def _invariant_index(data: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    index: dict[str, tuple[str, dict[str, Any]]] = {}
    for section, items in (("node", data.get("nodes", [])), ("edge", data.get("edges", []))):
        for item in items:
            if isinstance(item, dict) and item.get("invariant_id"):
                index[str(item["invariant_id"])] = (section, item)
    root = data.get("root_cause_criterion")
    if isinstance(root, dict) and root.get("invariant_id"):
        index[str(root["invariant_id"])] = ("root", root)
    return index


def _edge_for_assertion(
    assertion: dict[str, Any], edge_ids: set[str]
) -> str | None:
    matches = [
        str(invariant_id)
        for invariant_id in assertion.get("invariants", [])
        if str(invariant_id) in edge_ids
    ]
    return matches[0] if len(matches) == 1 else None


def _paths_to_sinks(verified_invariants: dict[str, Any]) -> list[list[str]]:
    nodes = {
        str(item.get("invariant_id")): item
        for item in verified_invariants.get("nodes", [])
        if isinstance(item, dict) and item.get("invariant_id")
    }
    edges = {
        str(item.get("invariant_id")): item
        for item in verified_invariants.get("edges", [])
        if isinstance(item, dict)
        and item.get("invariant_id")
        and item.get("from")
        and item.get("to")
    }
    incoming: dict[str, list[str]] = {}
    for edge_id, edge in edges.items():
        incoming.setdefault(str(edge["to"]), []).append(edge_id)
    sinks = sorted(
        node_id
        for node_id, node in nodes.items()
        if str(node.get("role") or "").lower() == "sink"
    )

    def walk(node_id: str, seen: set[str]) -> list[list[str]]:
        paths: list[list[str]] = []
        for edge_id in sorted(incoming.get(node_id, [])):
            edge = edges[edge_id]
            source = str(edge["from"])
            if source in seen:
                continue
            prefixes = walk(source, seen | {source})
            if prefixes:
                paths.extend(prefix + [edge_id] for prefix in prefixes)
            else:
                paths.append([edge_id])
        return paths

    paths = [path for sink in sinks for path in walk(sink, {sink})]
    return sorted(paths, key=lambda path: (-len(path), path))


def _reach_spec(
    ground_truth: dict[str, Any],
    reachability: dict[str, Any],
    public_text: str,
) -> dict[str, Any]:
    if not all(
        reachability.get(field) is True
        for field in (
            "R3_sink_function_reached",
            "R4_sink_line_reached",
            "R5_sanitizer_triggered",
        )
    ):
        raise ValueError("Reach unavailable: sink reachability is not fully verified")
    sink = ground_truth.get("sink")
    if not isinstance(sink, dict) or not str(sink.get("code") or "").strip():
        raise ValueError("Reach unavailable: ground_truth sink has no source operation")
    answer = str(sink["code"]).strip().rstrip(";")
    if explicitly_leaked(answer, public_text):
        raise ValueError("Reach unavailable: verified sink operation is explicit in public context")
    location = f"{sink.get('function', '<function>')} in {sink.get('file', '<file>')}"
    return {
        "id": "reach",
        "dimension": "Reach",
        "assertions": [],
        "invariants": [],
        "answer_mode": "exact",
        "statement": answer,
        "context": (
            f"Ask for the exact source-level operation at {location} that produces the "
            "sanitizer-reported sink. Required blank: ___."
        ),
        "blank_tokens": ["___"],
    }


def _mechanism_spec(
    verified_assertions: dict[str, Any],
    verified_invariants: dict[str, Any],
    public_text: str,
) -> dict[str, Any]:
    root = verified_invariants.get("root_cause_criterion")
    root_id = str(root.get("invariant_id") or "") if isinstance(root, dict) else ""
    candidates = [
        item
        for item in verified_assertions.get("assertions", [])
        if isinstance(item, dict)
        and item.get("kind") == "required"
        and root_id in {str(value) for value in item.get("invariants", [])}
    ]
    for assertion in candidates:
        op_name, left, right = assertion["check"]
        variable = root.get("variable") if isinstance(root, dict) else None
        answer = (
            f"{variable} {_SYMBOLS[op_name]} {_format_operand(right)}"
            if variable
            else format_check([op_name, left, right])
        )
        if explicitly_leaked(answer, public_text):
            continue
        location = str(assertion.get("at") or "the vulnerability-relevant decision")
        return {
            "id": "mechanism",
            "dimension": "Mechanism",
            "assertions": [str(assertion["id"])],
            "invariants": list(assertion.get("invariants", [])),
            "answer_mode": "required_atom",
            "statement": answer,
            "context": (
                f"Ask for the complete missing source-level condition on {variable or 'the named value'} "
                f"at {location}. Do not reveal the relation family or constant. Required blank: ___."
            ),
            "blank_tokens": ["___"],
        }
    raise ValueError("Mechanism unavailable: no non-leaked verified required assertion")


def _propagation_spec(
    verified_assertions: dict[str, Any],
    verified_invariants: dict[str, Any],
    public_text: str,
) -> dict[str, Any]:
    edges = {
        str(item.get("invariant_id")): item
        for item in verified_invariants.get("edges", [])
        if isinstance(item, dict) and item.get("invariant_id")
    }
    transitions: dict[str, dict[str, Any]] = {}
    for assertion in verified_assertions.get("assertions", []):
        if not isinstance(assertion, dict) or assertion.get("kind") != "transition":
            continue
        edge_id = _edge_for_assertion(assertion, set(edges))
        if edge_id:
            transitions[edge_id] = assertion
    for path in _paths_to_sinks(verified_invariants):
        if not path or any(edge_id not in transitions for edge_id in path):
            continue
        assertions = [transitions[edge_id] for edge_id in path]
        answers = [format_check(assertion["check"]) for assertion in assertions]
        if any(explicitly_leaked(answer, public_text) for answer in answers):
            continue
        blank_tokens = [f"___P{index}___" for index in range(1, len(path) + 1)]
        event_path = " -> ".join(
            f"{assertion['from']} to {assertion['at']}" for assertion in assertions
        )
        return {
            "id": "propagation",
            "dimension": "Propagation",
            "assertions": [str(assertion["id"]) for assertion in assertions],
            "invariants": path,
            "answer_mode": "ordered_relations",
            "statement": " -> ".join(answers),
            "context": (
                f"Ask for the ordered cross-event relations along {event_path}. "
                f"Use these named blanks exactly once and in order: {', '.join(blank_tokens)}."
            ),
            "blank_tokens": blank_tokens,
        }
    raise ValueError("Propagation unavailable: no non-leaked verified transition path reaches a sink")


def _probe_specs(
    verified_assertions: dict[str, Any],
    verified_invariants: dict[str, Any],
    ground_truth: dict[str, Any],
    reachability: dict[str, Any],
    public_text: str,
) -> list[dict[str, Any]]:
    if verified_assertions.get("schema_version") != "verified-assertions-v3":
        raise ValueError("probe generation requires verified-assertions-v3")
    return [
        _reach_spec(ground_truth, reachability, public_text),
        _mechanism_spec(verified_assertions, verified_invariants, public_text),
        _propagation_spec(verified_assertions, verified_invariants, public_text),
    ]


def build_question_request(
    verified_assertions: dict[str, Any],
    verified_invariants: dict[str, Any],
    ground_truth: dict[str, Any],
    reachability: dict[str, Any],
    public_text: str,
) -> dict[str, Any]:
    specs = _probe_specs(
        verified_assertions,
        verified_invariants,
        ground_truth,
        reachability,
        public_text,
    )
    return {
        "items": [
            {key: spec[key] for key in ("id", "statement", "context")}
            for spec in specs
        ]
    }


def _check_question_blanks(spec: dict[str, Any], question: str) -> None:
    expected = spec["blank_tokens"]
    named = re.findall(r"___P\d+___", question)
    if expected == ["___"]:
        if question.count("___") != 1 or named:
            raise ValueError(f"question {spec['id']} must contain exactly one ___")
        return
    if named != expected or any(question.count(token) != 1 for token in expected):
        raise ValueError(
            f"question {spec['id']} must contain named blanks {expected} exactly once in order"
        )


def finalize_questions(
    verified_assertions: dict[str, Any],
    verified_invariants: dict[str, Any],
    ground_truth: dict[str, Any],
    reachability: dict[str, Any],
    public_text: str,
    rendered: dict[str, Any],
) -> dict[str, Any]:
    specs_list = _probe_specs(
        verified_assertions,
        verified_invariants,
        ground_truth,
        reachability,
        public_text,
    )
    eligible_ids = [item["id"] for item in specs_list]
    specs = {item["id"]: item for item in specs_list}
    questions: dict[str, str] = {}
    for item in rendered.get("questions", []):
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("id") or "")
        question = str(item.get("question") or "").strip()
        if question_id in questions:
            raise ValueError(f"duplicate rendered question id: {question_id}")
        questions[question_id] = question
    if set(questions) != set(eligible_ids):
        raise ValueError(
            "rendered question ids do not match the three dimensions: "
            f"expected={eligible_ids}, got={sorted(questions)}"
        )
    probes = []
    for probe_id in eligible_ids:
        spec = specs[probe_id]
        question = questions[probe_id]
        _check_question_blanks(spec, question)
        if any(term in question.lower() for term in _HINT_TERMS):
            raise ValueError(f"question {probe_id} contains an answer-form hint")
        probes.append(
            {
                "id": probe_id,
                "dimension": spec["dimension"],
                "assertions": spec["assertions"],
                "invariants": spec["invariants"],
                "answer_mode": spec["answer_mode"],
                "question": question,
                "answer": spec["statement"],
            }
        )
    return {
        "schema_version": "reasoning-probes-v1",
        "sample_id": verified_assertions.get("sample_id"),
        "probes": probes,
    }


def _load_agent_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("questioning agent output must be one JSON object")
    return value


def render_with_agent(
    *,
    verified_assertions: dict[str, Any],
    verified_invariants: dict[str, Any],
    ground_truth: dict[str, Any],
    reachability: dict[str, Any],
    public_text: str,
    agent_command: str,
    role_file: Path,
    timeout: int = 600,
) -> dict[str, Any]:
    request = build_question_request(
        verified_assertions,
        verified_invariants,
        ground_truth,
        reachability,
        public_text,
    )
    with tempfile.TemporaryDirectory(prefix="questioning_agent_") as tmp:
        tmp_dir = Path(tmp)
        request_path = tmp_dir / "request.json"
        response_path = tmp_dir / "response.json"
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        command = [
            *shlex.split(agent_command),
            "--role-file",
            str(role_file),
            "--input",
            str(request_path),
            "--output",
            str(response_path),
        ]
        subprocess.run(command, check=True, timeout=timeout)
        return finalize_questions(
            verified_assertions,
            verified_invariants,
            ground_truth,
            reachability,
            public_text,
            _load_agent_json(response_path),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assertions", type=Path, required=True)
    parser.add_argument("--invariants", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--reachability", type=Path, required=True)
    parser.add_argument("--sample-info", type=Path, required=True)
    parser.add_argument("--default-crash-trace", type=Path, required=True)
    parser.add_argument("--agent-command", required=True)
    parser.add_argument("--role-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    sample_info = json.loads(args.sample_info.read_text(encoding="utf-8"))
    public_text = public_context_text(
        sample_info,
        args.default_crash_trace.read_text(encoding="utf-8", errors="replace"),
    )
    probes = render_with_agent(
        verified_assertions=json.loads(args.assertions.read_text(encoding="utf-8")),
        verified_invariants=json.loads(args.invariants.read_text(encoding="utf-8")),
        ground_truth=json.loads(args.ground_truth.read_text(encoding="utf-8")),
        reachability=json.loads(args.reachability.read_text(encoding="utf-8")),
        public_text=public_text,
        agent_command=args.agent_command,
        role_file=args.role_file,
        timeout=args.timeout,
    )
    args.out.write_text(json.dumps(probes, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
