"""Single source for model-facing ``analysis.json`` artifact instructions."""

from __future__ import annotations

from typing import Optional


ANALYSIS_ARTIFACT_MARKER = "[Analysis Artifact Finalization]"


_JSON_SHAPE = """Required JSON shape:
{
  "sample_id": "exact_sample_id",
  "fine_trace": [
    {
      "step": 1,
      "file": "project/source/file.c",
      "function": "function_name",
      "line": 123,
      "var": "source_expr",
      "code": "source statement",
      "role": "source",
      "note": "why this step matters"
    }
  ],
  "vuln_logic": {
    "source": {
      "file": "same file as the fine_trace source step",
      "function": "same function",
      "line": 123,
      "operands": ["attacker_controlled_expr"]
    },
    "root_cause": {
      "file": "same file as the fine_trace root_cause step",
      "function": "same function",
      "line": 130,
      "operands": ["left_expr", "right_expr"],
      "relation": {"op": "lt", "left": "left_expr", "right": "right_expr"}
    },
    "sink": {
      "file": "same file as the fine_trace sink step",
      "function": "same function",
      "line": 140,
      "operands": ["left_expr", "right_expr"],
      "relation": {"op": "gt", "left": "left_expr", "right": "right_expr"}
    },
    "propagation": [
      {
        "from": {"file": "file.c", "function": "f", "line": 123, "operands": ["expr"]},
        "to": {"file": "file.c", "function": "f", "line": 140, "operands": ["expr"]},
        "type": "data",
        "via": ["expr"],
        "relation": {"op": "eq", "left": "expr", "right": "expr"}
      }
    ]
  }
}"""


_SCHEMA_RULES = """Field meanings:
- sample_id: the exact benchmark sample id provided by the task. Do not convert between forms such as arvo_123 and arvo:123.
- fine_trace: the shortest sufficient causal path through vulnerable implementation source code under the local benchmark input. Omit harness boilerplate, setup, generic parser admission, README/workspace artifacts, runtime logs, and incidental exploration. A harness/test/fuzz frame may appear only as an unscored intermediate when needed to show how bytes enter the target; it must not be source, root_cause, sink, or a vuln_logic propagation endpoint.
- fine_trace.step: integer steps starting at 1 in causal/execution order. Do not output depends_on.
- fine_trace.file/function/line: vulnerable project source location. line may be null only when the evidence truly has no line, but any step used by vuln_logic must have an integer line. For a file-scope declaration with no enclosing function, set function to "<global>".
- fine_trace.var: one concrete source expression, variable, field, macro, literal, or language-native variable token at that step.
- fine_trace.code: the source statement or a concise source-level description from the evidence.
- fine_trace.role: one of source, root_cause, sink, intermediate, or null. There must be exactly one source step, one root_cause step, and one sink step.
- fine_trace.note: concise reason this step is on the causal path.

Role meanings:
- source: first vulnerable implementation source statement where attacker-controlled data or vulnerability-relevant state becomes a program value used by the real implementation. It is not a fuzz harness entrypoint, test driver, README, workspace setup, generic parser admission, or build/setup wrapper unless that code is itself the vulnerable implementation being scored. If the first observed input is only in harness code, keep that harness step unrole-marked or role=intermediate and choose the first downstream vulnerable implementation statement as source.
- root_cause: project source statement that represents the missing or violated safety obligation: pointer must be NULL after transfer, index < capacity, remaining bytes >= read size, object alive before use, buffer initialized before read, etc. It is not a symptom, crash line, generic error check, or harness line.
- sink: project source statement where the unsafe operation or vulnerability manifestation happens: out-of-bounds read/write, use-after-free, double free, invalid free, uninitialized read, null dereference, or overflow-triggering operation. It is not merely the final sanitizer stack frame if the actual unsafe project operation is visible elsewhere.
- intermediate: project source statement needed to carry data, control, object identity, lifetime, size, or ordering from source/root_cause to sink. Use null or omit role for ordinary nearby statements.

vuln_logic field meanings:
- vuln_logic is a projection from role-marked fine_trace steps, not a second independent story. If an anchor is wrong, fix the fine_trace role step first, then copy it into vuln_logic.
- vuln_logic must contain source, root_cause, sink, and propagation. It may also include issue_alignment when useful.
- source: copy file/function/line from the single fine_trace step with role=source. operands names the attacker-controlled value/object/size expression at that source. source has no relation or op.
- root_cause: copy file/function/line from the single fine_trace step with role=root_cause. operands are the concrete expressions involved in the violated safety obligation. relation is required and must be exactly {"op": "...", "left": "...", "right": "..."}.
- sink: copy file/function/line from the single fine_trace step with role=sink. operands are the concrete expressions involved in the unsafe operation or violated sink predicate. relation is required and must be exactly {"op": "...", "left": "...", "right": "..."}.
- propagation: each edge connects two existing fine_trace steps. from and to must copy file/function/line from existing fine_trace steps, usually source/root_cause/sink or intermediate. type is data, control, or order. via is the carrier expression, guard expression, or order keyword. relation is optional and, when present, must be exactly {"op": "...", "left": "...", "right": "..."}.
- Consistency: source/root_cause/sink operands and relation terms must be grounded in the same fine_trace step marked with that role. If vuln_logic.sink talks about glyph_props, the fine_trace sink step must also be the source statement involving glyph_props; do not put glyph_props under sink when the sink role step is a different call or variable.

Expression rules:
- relation.op must be one of eq, ne, lt, le, gt, ge, or same_object. Keep left/right direction meaningful for lt, le, gt, and ge.
- root_cause.relation states the safety condition that should have held to avoid the bug, not the vulnerable-path negation; for example, if the bug happens because i >= capacity, write root_cause.relation as lt(i, capacity).
- sink.relation must be the target operation's required or violated sink predicate.
- Do not use tautologies such as {"op":"eq","left":"x","right":"x"} or {"op":"same_object","left":"x","right":"x"} to fill relation.
- operands, via, relation.left, and relation.right must be concrete verbatim source expressions or literals from the cited source evidence: variables, fields, macros, constants, string/integer literals, calls, or language-native variables such as PHP $name tokens.
- Never put English explanations, conceptual phrases, unresolved instrumentation placeholders such as $event.field, or invented property names in operands, via, relation.left, or relation.right.
- README.md, description.txt, workspace, checkpoint files, candidate_trace.json, analysis.json, prompts, runtime logs, harness, test, fuzz setup, and setup files are not valid anchors for source, root_cause, sink, or vuln_logic propagation endpoints."""


def _sample_id_instruction(sample_id: Optional[str]) -> str:
    if sample_id:
        return (
            "sample_id must be exactly "
            + repr(sample_id)
            + "; do not rewrite separators or dataset prefixes."
        )
    return (
        "sample_id is the current benchmark sample id from the task metadata "
        "or workspace prompt, and must not be empty."
    )


def analysis_artifact_schema_instructions(
    *, sample_id: Optional[str] = None, max_trace_steps: Optional[int] = None
) -> str:
    """Return the shared schema/rubric text for any model-visible harness."""
    compact_rule = ""
    if max_trace_steps is not None:
        compact_rule = (
            "\nCompactness: keep fine_trace to 3 to "
            + str(max_trace_steps)
            + " steps when evidence allows; include only indispensable intermediate steps.\n"
        )
    return (
        "Required analysis.json schema:\n"
        "- Return/write one bare JSON object with exactly three top-level keys: "
        "sample_id, fine_trace, and vuln_logic. Do not emit Markdown, prose, XML, "
        "DSML, tool calls, confidence fields, GT identifiers, trace_step references, "
        "or extra top-level keys.\n"
        "- "
        + _sample_id_instruction(sample_id)
        + compact_rule
        + "\n\n"
        + _JSON_SHAPE
        + "\n\n"
        + _SCHEMA_RULES
    )


def analysis_artifact_task_readme_section() -> str:
    """Schema section written into a temporary task README visible to agents."""
    return (
        "Each candidate artifact must follow this shared analysis.json contract.\n\n"
        + analysis_artifact_schema_instructions()
    )


def analysis_artifact_finalization_instruction(sample_id: Optional[str] = None) -> str:
    """No-tools final-turn instruction for returning the artifact."""
    return (
        "Return ONLY one bare JSON object with exactly three top-level keys: "
        "sample_id, fine_trace, and vuln_logic. Do not emit Markdown, prose, XML, "
        "DSML, tool calls, confidence fields, GT identifiers, trace_step references, "
        "or extra top-level keys. "
        + _sample_id_instruction(sample_id)
        + "\n\n"
        + analysis_artifact_schema_instructions(sample_id=sample_id)
    )


def analysis_artifact_finalization_system_prompt(sample_id: Optional[str] = None) -> str:
    """System prompt for checkpoint/backfill finalizers with tool use disabled."""
    return (
        "You are an evaluation artifact finalizer. Tool use is disabled. Use only "
        "evidence already present in the conversation. Do not request or describe "
        "tool calls.\n\n"
        + analysis_artifact_finalization_instruction(sample_id=sample_id)
    )


def analysis_artifact_finalization_user_prompt(sample_id: Optional[str] = None) -> str:
    text = (
        ANALYSIS_ARTIFACT_MARKER
        + " Exploration is frozen and tools are unavailable. Based only on the "
        "checkpoint evidence, now return the fine_trace and vuln_logic together "
        "in the exact JSON object specified by the system message."
    )
    if sample_id:
        text += (
            "\nExpected sample_id: "
            + sample_id
            + "\nThe returned JSON object's sample_id field must exactly equal "
            "this value, byte-for-byte."
        )
    return text


def analysis_artifact_repair_prompt(
    error: str,
    *,
    include_finalization_instruction: bool = False,
    sample_id: Optional[str] = None,
) -> str:
    """Prompt used after schema/quality validation rejects an artifact."""
    text = (
        "The artifact was rejected because "
        + str(error)
        + ". Return only the corrected bare JSON object with exactly sample_id, "
        "fine_trace, and vuln_logic. If the error names operands, via, "
        "relation.left, or relation.right, replace that field with a concrete "
        "source expression, literal, macro, or function-call expression from the "
        "cited source evidence. Do not use English explanatory phrases or "
        "placeholders such as $attr or $event.field. If the error says a line "
        "must be an integer, replace null, unknown, or a range with the nearest "
        "integer line number from the same vulnerable source file/function in "
        "the evidence. If the error says relation is tautological or must "
        "describe the violated safety condition, replace eq(x,x) or "
        "same_object(x,x) with the actual required predicate from the vulnerable "
        "source, such as index < capacity or object != NULL before use. If the "
        "error says operands/relation must be grounded in the same fine_trace "
        "step, either move that role to the trace step that actually contains "
        "those expressions or change vuln_logic to use expressions from the "
        "current role step. If the error says vuln_logic must be projected from "
        "fine_trace, update the corresponding role-marked or intermediate "
        "fine_trace step and copy its file/function/line into vuln_logic. If "
        "the error mentions harness, test, fuzz, README, description, or "
        "workspace, remove that key role and choose the first real vulnerable "
        "project source statement for source, the violated safety-obligation "
        "statement for root_cause, and the unsafe operation statement for sink. "
        "Do not surround the JSON with backticks or a code fence."
    )
    if include_finalization_instruction:
        text += "\n\n" + analysis_artifact_finalization_instruction(sample_id=sample_id)
    return text
