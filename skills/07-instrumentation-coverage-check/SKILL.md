---
name: instrumentation-coverage-check
description: Check whether source, sink, root-cause, and trace locations in generated memory-safety GT are actually covered by the PoC. Use this after a draft GT exists and before final validation.
---

# Instrumentation Coverage Check

## Purpose

Measure whether GT locations are executed by the trigger. This checks precision of the generated GT.

This skill does not prove recall. A location not instrumented cannot be judged.

## Inputs

- Vulnerable source checkout.
- Draft `ground_truth.json`.
- Trigger command and PoC.
- Build information.
- Existing sanitizer or Valgrind reproduction setup.

## Default Backend

Prefer `gdb_breakpoint` instrumentation because it is less invasive:

- Set breakpoints at GT file/line locations.
- Print a stable `[GT-HIT]` marker.
- Continue execution.
- Run the trigger.

Use source patch instrumentation only when debugger breakpoints are not practical.

## Locations to Check

Instrument locations from:

- `source`
- `sink`
- `root_cause`
- `trace`

Deduplicate identical file/line/function triples.

## Outputs

Do not keep a standalone coverage JSON in final results unless the user asks.

Instead:

- Append hit/miss details to `generation.log`.
- Update `sample_state.json` coverage fields:
  - `checked`
  - `covered_gt_locations`
  - `missing_gt_locations`

## Interpretation

- All or most critical locations hit: GT precision is supported.
- Sink hit but source missing: likely source definition is wrong or trigger path differs.
- Source hit but sink missing: likely reproduction command differs or GT sink is wrong.
- Intermediate misses: mark warning; inspect for conditional paths or hallucinated steps.

## Failure Conditions

Use these failure types:

- `instrumentation_failed`
- `instrumentation_not_covered`
- `needs_human_review`
