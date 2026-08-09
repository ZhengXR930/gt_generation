# Role: Stage 04B Assertion Execution

You are a fresh isolated coding-agent CLI session. Execute the already frozen
assertion and instrumentation plan. Do not change `candidate_assertions.json`,
`candidate_invariants.json`, `field_bindings.json`, `event_locations.json`,
either instrumentation patch, `.assertion_spec_frozen.json`, or
`assertion_preflight.json`.

## Required outputs

- `vulnerable_assertion_trace.txt`
- `fixed_assertion_trace.txt`
- `assertion_results.json`
- `perturbation_results.json`
- `verified_assertions.json`
- `verified_invariants.json`

Before execution, verify `assertion_preflight.json`,
`vulnerable_instrumentation_preflight.json`, and
`fixed_instrumentation_preflight.json` all have `ok: true` and bind the current
assertion hash and patch hashes. If a frozen patch unexpectedly fails during
execution, report which side failed; rerun only that side's instrumentation
stage. Do not edit the plan or either patch here.

## Execution order

1. Apply the frozen vulnerable instrumentation, build, and run the original PoC.
   Observed assertions must hold, and required obligations must be violated when
   their protected operation executes.
2. Restore/switch to the true fixed side, apply the frozen fixed
   instrumentation, build, and run the same PoC.
3. Classify each required assertion as `genuine`, `guarded`, or
   `not_exercised`. If the fixed original is guarded or vacuous, run only the
   closest source-grounded PoC perturbation needed to obtain a genuine witness.
   Stop after the first valid witness; do not sweep values.
4. Keep only runtime-verified candidate invariants in
   `verified_invariants.json`.
5. Run the deterministic assertion and binding gates:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit assertions \
  --spec <result_dir>/candidate_assertions.json \
  --vulnerable-trace <result_dir>/vulnerable_assertion_trace.txt \
  --fixed-trace <result_dir>/fixed_assertion_trace.txt \
  --verified-invariants <result_dir>/verified_invariants.json \
  --sanitizer-trace <result_dir>/sanitizer_trace.txt \
  --results-out <result_dir>/assertion_results.json \
  --perturbation-results-out <result_dir>/perturbation_results.json \
  --verified-assertions-out <result_dir>/verified_assertions.json

PYTHONPATH=gt_generation python3 -m gt_toolkit assertions \
  --check-bindings-only \
  --spec <result_dir>/candidate_assertions.json \
  --verified-invariants <result_dir>/verified_invariants.json \
  --field-bindings <result_dir>/field_bindings.json \
  --event-locations <result_dir>/event_locations.json
```

Do not run reachability and do not clean the ARVO container or images. The next
deterministic stage owns debugger reachability, and cleanup occurs only after
the complete workflow exits.

## ARVO lifecycle

Use the existing `gt_toolkit arvo-workspace` commands:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> apply-instrumentation \
  --patch <result_dir>/vulnerable-instrumentation.patch
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> compile-target --version vulnerable
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> run --version vulnerable --expect crash \
  --case-name original
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> switch-fixed-image
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> apply-instrumentation \
  --patch <result_dir>/fixed-instrumentation.patch
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> compile-fixed
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> run --version fixed --expect clean \
  --case-name original
```

The `run` command owns the `CASE ...` / `ENDCASE` framing in both assertion
trace files. Do not hand-write, replace, or post-process either trace.

The published `-fix` image is always the ARVO fixed-side oracle. Run the exact
same PoC on the vulnerable and fixed images. `patch.diff` is explanatory
metadata only: do not apply it, derive the fixed source or invariant from it, or
accept it as evidence that the vulnerability was repaired.

## Repo-track lifecycle

Run every build and target invocation through `<result_dir>/build.sh`; paths
inside that environment are under `/gt`. Reuse the reproduction command from
`reproduction_report.json`. Never run the target directly on the host.
