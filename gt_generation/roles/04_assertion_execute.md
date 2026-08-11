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

`assertion_reward_spec.json` is an optional reward-framework projection. Do not
let its absence block a GT package whose core assertion, invariant, and
reachability evidence is complete.

Do not add artifact-level `schema_version` to `verified_assertions.json`,
`verified_invariants.json`, `field_bindings.json`, or `event_locations.json`.
The only schema-versioned file in this stage is the frozen assertion spec
protocol (`candidate_assertions.json` / `.assertion_spec_frozen.json`).

Before execution, read `prepare_report.json` and choose the matching lifecycle:
ARVO samples use `gt_toolkit arvo-workspace`; repo-track samples
(`prepare_report.track` starts with `repo/`) use `<result_dir>/build.sh`.
Verify `assertion_preflight.json`,
`vulnerable_instrumentation_preflight.json`, and
`fixed_instrumentation_preflight.json` all have `ok: true`, bind the current
assertion hash and patch hashes, and report the expected track. A repo-track
preflight report must have `track` starting with `repo/`; an ARVO preflight
must not be reused for a repo-track sample. If a frozen patch unexpectedly fails
during execution, report which side failed; rerun only that side's
instrumentation stage. Do not edit the plan or either patch here.

## Execution order

1. Apply the frozen vulnerable instrumentation, build, and run the original PoC.
   Required root obligations must be violated when their protected operation
   executes. Propagation-node `observed` assertions and propagation-edge
   `transition` assertions are accepted only when they hold in this real
   vulnerable execution; assertions that do not hold are omitted from the final
   verified subset instead of blocking a package whose root differential is
   proven.
2. Restore/switch to the true fixed side, apply the frozen fixed
   instrumentation, build, and run the same PoC. The fixed run is required for
   the root `required` differential; fixed-side outcomes of `observed` and
   `transition` assertions are diagnostic only and do not gate propagation
   verification.
3. Classify each required assertion as `genuine`, `guarded`, `avoided`, or
   `not_exercised`. If the fixed original is `guarded` or `avoided` because the
   protected operation did not execute, run exactly one closest source-grounded
   PoC perturbation to obtain a genuine witness of normal execution after the
   guard. This is a hard maximum of one non-original case across the fixed
   trace: never try a second fallback, enumerate values, or fuzz. Stop
   immediately after that one case, whether it succeeds or fails.
4. Keep only runtime-verified candidate invariants in
   `verified_invariants.json`. Preserve the GT contract shape from
   `candidate_invariants.json`: `root_cause_criterion` is only a pointer to the
   `role: "root_cause"` node, every node/edge keeps `operands` and structured
   `relation`, and every edge keeps `from_node`/`to_node` references. The
   root-cause criterion is mandatory; propagation nodes and edges may be a
   verified subset of the candidate graph.
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
  --verified-assertions-out <result_dir>/verified_assertions.json \
  --field-bindings <result_dir>/field_bindings.json \
  --event-locations <result_dir>/event_locations.json \
  --ground-truth <result_dir>/ground_truth.json

PYTHONPATH=gt_generation python3 -m gt_toolkit assertions \
  --check-bindings-only \
  --spec <result_dir>/candidate_assertions.json \
  --verified-invariants <result_dir>/verified_invariants.json \
  --field-bindings <result_dir>/field_bindings.json \
  --event-locations <result_dir>/event_locations.json
```

If the fixed original was `guarded` or `avoided`, include exactly one
non-original CASE in the fixed trace for the closest source-grounded
perturbation. Keep its raw runtime events as normal `CASE`/`ASSERT_EVT`
records; do not hand-edit CASE framing.

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
inside that environment are under `/gt`. Reuse the setup and reproduction
commands from `reproduction_report.json`; if those commands were recorded as
`<result_dir>/build.sh '<inner command>'`, execute only the inner command
through the current `<result_dir>/build.sh`. Never run the target directly on
the host.

Use the deterministic repo runner so the toolkit owns reset, fixed checkout,
patch application, setup, target invocation, and `CASE ... ENDCASE` framing:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit repo-workspace run \
  --result-dir <result_dir> \
  --version vulnerable \
  --patch <result_dir>/vulnerable-instrumentation.patch \
  --expect crash \
  --case-name original

PYTHONPATH=gt_generation python3 -m gt_toolkit repo-workspace run \
  --result-dir <result_dir> \
  --version fixed \
  --patch <result_dir>/fixed-instrumentation.patch \
  --expect clean \
  --case-name original
```

Do not hand-write, replace, or post-process either repo-track assertion trace.
If the fixed original is `guarded` or `avoided`, add exactly one closest
source-grounded perturbation case to the same fixed trace with
`repo-workspace run --version fixed --append-trace --case-name <name> ...` and
stop.
