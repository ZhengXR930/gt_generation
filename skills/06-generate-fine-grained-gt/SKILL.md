---
name: generate-fine-grained-gt
description: Generate Veritas-style fine-grained ground truth for a reproduced memory-safety vulnerability. Use this after traces, source code, issue description, PoC, and patch diff are available.
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

Use the Veritas-style structure:

```json
{
  "ground_truth": {
    "vulnerabilities": [
      {
        "vuln_id": "",
        "class": "",
        "cwe": "",
        "before_URL": "",
        "before_commit": "",
        "after_commit": "",
        "project_id": "",
        "severity": null,
        "sink": {},
        "source": {},
        "trace_hint": [],
        "call_chain": [],
        "binary_gt": [],
        "slice_gt": {},
        "data_flow_chain": [],
        "root_cause": {},
        "poc": {},
        "patch": []
      }
    ]
  }
}
```

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

## Data Flow Chain

`data_flow_chain` must be step-by-step and include:

- Input acquisition.
- Relevant parsing or decoding transitions.
- State updates that preserve attacker influence.
- Bounds, lifetime, allocation, or initialization mistake.
- Final invalid memory operation.

Include indirect calls, callbacks, dispatch tables, or virtual calls when they are part of the real path.

## Patch Usage

Use patch diff as oracle evidence to confirm the root cause and fix intent.

Do not make the GT a patch explanation only. The GT must explain vulnerable-version behavior under the PoC.

## Quality Bar

The GT should let a reviewer answer:

- What attacker-controlled data enters where?
- How does it reach the sink?
- Why is the operation unsafe?
- Which code statement is the root cause?
- How do the traces and patch support this explanation?

If any of these cannot be answered, write `partial_ground_truth.json` and mark `needs_human_review`.

