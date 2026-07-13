# GT Generation Agent Contract

This file defines the execution boundary for role prompts. It is short enough
to include as optional global context, but each role prompt is still the primary
instruction for an agent run.

## Inputs

Each sample already provides or references:

- CVE/public vulnerability id
- issue description
- vulnerable codebase or repository + vulnerable commit
- PoC / PoV / crash input
- `patch.diff`
- optional trigger metadata

Do not treat web search or dataset discovery as a normal role responsibility.

## Execution Environment

Run build/reproduction work inside the configured Docker environment. Install
project-specific dependencies in the sample `build.sh`, not in the global image.

Available tools may include sanitizer builds, Valgrind, GDB, compilers, and
standard build tools.

## Role Boundary

- Reproducer writes runtime artifacts only.
- GT Generator writes `ground_truth.json` semantics only.
- Static Reviewer writes `static_review.json`.
- Runtime Validator reads the GT to select+verify the invariant points, writing `verified_invariants.json` + `reachability_report.json`.
- Runtime Validator writes `reachability_report.json`.

Do not write model-authored grounding labels into `ground_truth.json`. Runtime
evidence belongs in traces and validation reports, not in agent-generated GT
trace steps.

`ground_truth.json` may include `poc.format` as a format/reachability contract
for candidate PoC evaluation. It must not include original PoC bytes, exploit
writeup text, or a target-specific exploit recipe.
