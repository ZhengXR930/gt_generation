---
name: validate-ground-truth
description: Validate a generated memory-safety ground_truth.json for schema, source/sink plausibility, trace support, patch consistency, and review readiness. Use this after GT generation and optional instrumentation coverage.
---

# Validate Ground Truth

## Purpose

Decide whether `ground_truth.json` is acceptable, incomplete, or requires human review.

This skill writes validation results into `sample_state.json` and `generation.log`; it does not need to keep a separate final validation report.

## Inputs

- `ground_truth.json` or `partial_ground_truth.json`.
- Vulnerable source checkout if still available.
- `sanitizer_trace.txt`.
- `valgrind_trace.txt`.
- Patch diff.
- `sample_state.json`.
- Coverage observations in `generation.log`.

## Required Checks

Check:

- Required top-level schema exists.
- Each vulnerability has `source`, `sink`, `call_chain`, `data_flow_chain`, and `root_cause`.
- Source and sink file/line/function fields are present.
- Source and sink lines exist in vulnerable source when source is available.
- Sink is supported by sanitizer or Valgrind trace.
- Root cause explanation is consistent with patch diff.
- Data-flow chain starts at attacker-controlled input and ends at the sink.
- Indirect call, callback, parser dispatch, or generated-code edges are not silently skipped when relevant.
- Normalized issue description is not contradicted by the reproduced crash.

## Status Outcomes

Set sample status to:

- `completed`: GT is complete and supported.
- `needs_human_review`: GT is mostly useful but has uncertainty.
- `failed`: GT is invalid or reproduction evidence is insufficient.

## Human Review Triggers

Use `needs_human_review` when:

- Source is plausible but not proven.
- Sink has multiple competing candidates.
- Valgrind and sanitizer traces disagree.
- Instrumentation misses a critical GT location.
- Patch indicates a different root cause than the trace.
- Data flow depends on complex indirect dispatch not fully resolved.

## Final Rule

Do not mark a sample `completed` only because it has a crash trace. Completion requires a coherent source-to-sink explanation supported by traces and source inspection.

