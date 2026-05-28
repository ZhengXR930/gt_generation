---
name: generate-fine-grained-gt
description: Generate fine-grained ground truth for a reproduced memory-safety vulnerability. Use this after traces, source code, issue description, PoC, and patch diff are available.
---

# Generate Fine-Grained GT

## Purpose

Produce a detailed `ground_truth.json` that explains the vulnerability from attacker-controlled input to memory-safety sink and root cause.

The GT should be suitable for evaluating coding agents on vulnerability reasoning.

## Inputs

- Vulnerable source checkout.
- `normalized_bug_description` and `original_bug_description`.
- `sanitizer_trace.txt`.
- `valgrind_trace.txt`.
- PoC/PoV and trigger command.
- Patch diff and fix commit as oracle context.
- Trace triage notes in `generation.log`.

## Required Output

Write:

```text
gt_results/<sample_id>/ground_truth.json
```

Use this structure. It should stay smaller than the Veritas wrapper format, but it must preserve explicit coarse trace, fine-grained data-flow evidence, and root-cause analysis:

```json
{
  "sample_id": "",
  "vuln_id": "",
  "project": {
    "id": "",
    "repo": "",
    "vulnerable_commit": "",
    "fixed_commit": ""
  },
  "classification": {
    "class": "",
    "cwe": "",
    "root_cause_cwe": ""
  },
  "source": {},
  "sink": {},
  "root_cause": {},
  "coarse_trace": [],
  "fine_trace": [],
  "root_cause_analysis": {
    "summary": "",
    "key_mechanism": "",
    "why_patch_works": ""
  },
  "poc": {}
}
```

Do not include `binary_gt`, `trace_hint`, `slice_gt`, `execution_trace`, `call_chain`, `data_flow_chain`, or inline `patch` summaries unless the user explicitly asks for Veritas-compatible expansion. Keep `patch.diff` as a separate artifact.

The main trace fields have different jobs:

- `coarse_trace`: function-level path. It should explain the important entry/dispatch/vulnerable-site transitions without drowning in stack frames.
- `fine_trace`: statement-level source-to-sink chain. It should name the variable/state, source line, role, code statement, and why it remains attacker-controlled or unsafe.
- `root_cause_analysis`: prose reasoning that explains the vulnerability mechanism and why the patch fixes the root cause.

## Source Definition

`source` is the point where attacker-controlled PoC/PoV input enters project-controlled code. It is not merely the first function in the crash stack.

Examples:

- File read into a buffer.
- Fuzzer input pointer entering a target function.
- Parser receiving bytes from the testcase.
- Network or archive input decoded from the PoC.

## Sink Definition

`sink` is the memory-safety operation where the invalid read/write/free/use occurs or the closest project-code statement responsible for it.

Use sanitizer and Valgrind traces to anchor the sink, then inspect source to avoid blaming runtime interceptors or library wrappers.

## Coarse Trace

`coarse_trace` is the function-level source-to-sink chain. It must include:

- Input acquisition.
- Relevant parsing or decoding transitions.
- State updates that preserve attacker influence.
- Bounds, lifetime, allocation, or initialization mistake.
- Final invalid memory operation.

Include indirect calls, callbacks, dispatch tables, or virtual calls when they are part of the real path.

Each step should include:

- `step`
- `file`
- `function`
- `role`
- `summary`

## Fine Trace

`fine_trace` is the statement-level chain for attacker-controlled data or state. It should be detailed enough to evaluate whether an agent recovered source, propagation, root cause, and sink, not merely named the crashing function.

Each step should include:

- `step`
- `file`
- `function`
- `line`
- `role`
- `var`
- `code`
- `note`

Use roles such as `source`, `entry`, `dispatch`, `indirect_call`, `tainted_read`, `tainted_value_materialization`, `bounds_state`, `lifetime_state`, `root_cause`, `unsafe_allocation`, `invalid_free`, `sink`, or a similarly precise role.

## Root Cause Analysis

`root_cause_analysis` must include:

- `summary`: concise but complete explanation of the vulnerability logic.
- `key_mechanism`: normalized mechanism name such as `unsigned_integer_overflow_in_allocation_size`, `stale_allocator_metadata_after_free`, or `missing_runtime_type_check`.
- `why_patch_works`: explain how the patch stops the vulnerability at the root-cause site. Do not copy a full diff here; keep the complete patch in `patch.diff`.

## Patch Usage

Use patch diff as oracle evidence to confirm the root cause and fix intent.

Do not copy or summarize the patch into `ground_truth.json`. The original diff should live in `final_dataset/pocs/<sample_id>/patch.diff` or another dataset material path. The GT must explain vulnerable-version behavior under the PoC.

## Quality Bar

The GT should let a reviewer answer:

- What attacker-controlled data enters where?
- How does it reach the sink?
- Why is the operation unsafe?
- Which code statement is the root cause?
- How do the traces and patch support this explanation?

If any of these cannot be answered, write `partial_ground_truth.json` and mark `needs_human_review`.
