# Role: Stage 04A Assertion Plan

You are a fresh isolated coding-agent CLI session. Enter only when
`static_review.json` has all four review booleans true. Convert the accepted
fine trace into a minimal, source-derived assertion plan. Do not execute the
target, run reachability, rewrite the GT, or inspect prior runtime assertion
results.

## Required outputs

- `candidate_assertions.json`
- `candidate_invariants.json`
- `field_bindings.json`
- `event_locations.json`
- `.assertion_spec_frozen.json`
- `assertion_preflight.json`

`sample_info.json`, `build.sh`, `poc`, `patch.diff`, and `ground_truth.json`
are immutable inputs.

## Plan

1. Select the semantically irreducible source-level subgraph needed to explain
   the missing root obligation, the unsafe operation, and every propagation
   hop where the carried value, alias, owner, or lifetime state changes.
2. Exclude ordinary reachability plumbing, duplicate observations, incidental
   PoC values, generic API facts, and all fuzz harness callbacks/helpers.
   Harness files and functions such as `LLVMFuzzerTestOneInput`, `fuzz/`,
   `fuzzer/`, `fuzzing/`, `ossfuzz/`, and `*_fuzzer.*` are unscored test
   boundaries. If an indispensable root or sink exists only there, report the
   GT quality error and do not invent a project-code anchor.
3. Compile one semantic assertion per selected invariant. A required assertion
   captures the missing root obligation. Every selected edge has exactly one
   `transition` assertion directly relating its source and target event fields.
4. Record every assertion operand in `field_bindings.json` as an exact
   vulnerable-source expression. Record every synthetic event in
   `event_locations.json` with its real vulnerable-source function, file, and
   line.
5. Stop at the semantic commitment. Do not create instrumentation patches,
   compile either version, or execute the target. Later isolated stages map this
   frozen plan onto the real vulnerable and fixed source independently.

The required assertion must be recoverable from the sanitizer trace and source,
not from `patch.diff`, heap addresses, allocator metadata, or PoC-only constants.
Do not read `patch.diff` to construct, select, or reject an invariant. Stage 01
already established the vulnerable/fixed PoC differential; this stage explains
the vulnerable execution using the accepted fine trace, sanitizer trace, and
vulnerable source.
For a guarded fix, `protects` names the dangerous operation whose absence will
be distinguished during execution.

## Freeze

Write a complete `assertion-spec-v3`, including its canonical `content_hash`,
then freeze it:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit assertions \
  --spec <result_dir>/candidate_assertions.json \
  --freeze-only \
  --freeze-marker <result_dir>/.assertion_spec_frozen.json
```

Run preflight over the semantic plan and its source bindings:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit assertion-preflight \
  --spec <result_dir>/candidate_assertions.json \
  --candidate-invariants <result_dir>/candidate_invariants.json \
  --field-bindings <result_dir>/field_bindings.json \
  --event-locations <result_dir>/event_locations.json \
  --out <result_dir>/assertion_preflight.json
```

Finish only when `assertion_preflight.json` has `ok: true`. Do not create
placeholder traces, verification results, verified invariants, or a
reachability report. Do not create either instrumentation patch; later
side-specific stages own those artifacts.
