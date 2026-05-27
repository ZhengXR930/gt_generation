---
name: reproduce-crash
description: Run a memory-safety PoC/PoV against sanitizer and Valgrind builds, save crash traces, and check whether the observed failure matches the sample issue description.
---

# Reproduce Crash

## Purpose

Execute the sample trigger against both instrumented builds and save independent crash evidence.

## Inputs

- `gt_results/<sample_id>/build.sh`
- Sanitizer binary or harness.
- Valgrind/debug binary or harness.
- `final_dataset/pocs/<sample_id>/poc` or equivalent PoC/PoV.
- `trigger.json` and `run.sh`.
- `normalized_bug_description`.

## Required Outputs

Write:

```text
gt_results/<sample_id>/sanitizer_trace.txt
gt_results/<sample_id>/valgrind_trace.txt
```

Append all executed commands and exit codes to `generation.log`.

## Matching Criteria

The observed crash should match the issue description by at least two of:

- Crash class, such as heap-use-after-free or out-of-bounds read.
- Project component or target binary.
- Function name from crash state or advisory.
- Sanitizer family.
- PoC path or testcase identity.

If only one trace reproduces but the evidence is strong, continue with `needs_human_review` rather than discarding the sample.

## Trace Capture Rules

- Capture stdout and stderr.
- Preserve full sanitizer stack traces.
- Preserve Valgrind invalid read/write/free reports and allocation/free contexts.
- Do not overwrite a successful trace with a later failed attempt.

## Failure Conditions

Use these failure types:

- `trigger_missing`
- `trigger_failed_no_crash`
- `trigger_crash_mismatch`
- `trace_too_shallow`
- `needs_human_review`

