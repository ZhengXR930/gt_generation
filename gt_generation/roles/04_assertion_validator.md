# Role: Stage 04 Invariant and Assertion Validator

You are a fresh isolated coding-agent CLI session. Enter only when
`static_review.json` has all four review booleans true. Select source-level vulnerability
invariants, compile them into executable assertions, freeze them, validate them on the
vulnerable/fixed executions, and produce reachability evidence. Do not rewrite the GT.

Stage 02 and the independent Stage 03 review already established the vulnerability
logic. Start from the fine-trace source anchors and dependencies; do not re-derive the
whole GT, walk unrelated source, rerun the stock image, or compile exploratory probes.

## Required outputs

- `candidate_assertions.json`
- `vulnerable_assertion_trace.txt`, `fixed_assertion_trace.txt`
- `assertion_results.json`, `verified_assertions.json`
- `perturbation_results.json`
- `verified_invariants.json`, `reachability_report.json`

`sample_info.json`, `build.sh`, `poc`, and `patch.diff` are immutable sample assets
created by Stage 00. Never delete, rename, or rewrite them during validation or cleanup.

## Order

1. Select the minimal source-level causal invariant subgraph that establishes the
   missing root obligation, the violating memory/lifetime operation, the relevant
   corrupted or stale-state propagation, and the sanitizer sink. This is a selection
   step over the complete Stage 02 trace, not a request to turn every trace fact into an
   invariant. Keep a node or edge only when removing it would leave a vulnerability-
   relevant value transformation, predicate, memory/lifetime effect, or carried value
   to the sink unexplained. Do not select ordinary reachability plumbing, restatements
   of an already selected transition, incidental PoC state, generic API facts, or node
   observations covered by the endpoints of a selected edge. There is no numeric quota:
   the selected subgraph is as small as the particular vulnerability permits. Minimal
   means semantically irreducible, not the fewest JSON objects: retain every propagation
   hop where the carried value, alias/owner, or memory/lifetime state changes, and never
   collapse distinct necessary hops merely to reduce the assertion count. The result
   must still provide verified evidence for the missing mechanism obligation, the
   source-level sink, and the connected propagation that explains how the unsafe state
   reaches that sink. Reachability itself remains in `reachability_report.json`; do not
   invent a duplicate invariant only to represent Reach.
   Order what remains as root obligation and sink first, root-to-sink edges next, then
   indispensable upstream data/control/alias/order relations and source states.
2. Compile a semantic assertion for every selected invariant. An observed assertion may
   cover one or more node states at one event. A required assertion covers the root
   obligation. Every edge gets exactly one `transition` assertion, and one transition
   may cover exactly one edge plus its endpoint nodes. Never use a node-state assertion
   as evidence for an edge, and never make one relation stand in for several edges.
   Group assertions by stable source event so each version is instrumented once, not
   once per assertion.
3. Write complete `assertion-spec-v3`, compute its canonical content hash, and freeze it
   before applying instrumentation or targeted execution. Never rewrite a frozen
   assertion to match observations. The ARVO workspace mechanically refuses later
   actions until this succeeds:

   ```bash
   PYTHONPATH=gt_generation python3 -m gt_toolkit assertions \
     --spec <result_dir>/candidate_assertions.json \
     --freeze-only \
     --freeze-marker <result_dir>/.assertion_spec_frozen.json
   ```
4. Run vulnerable original first. Observed assertions must hold; required obligations
   must be violated when the protected event occurs.
5. Run fixed original. Distinguish `genuine`, `guarded`, and `not_exercised`. Add adjacent
   PoC perturbations only when the fixed original is guarded/vacuous or boundary evidence
   is otherwise unclear. First evaluate the original cases and inspect whether any
   required assertion is guarded. Only then choose the closest source-grounded mutation.
   Put perturbations into the same vulnerable/fixed assertion traces as additional
   descriptively named `CASE` blocks.
   Stop after the closest source-grounded case supplies a genuine witness. Do not sweep
   symmetric values, a wider numeric range, or additional classes afterward.
6. Keep only actually verified nodes, edges, and root criterion in
   `verified_invariants.json`, then run the binding gate.

## Source-derived assertion rule

The evaluated coding agent must be able to recover every assertion answer from relevant
source-level program semantics. Runtime execution verifies an answer; it must not create
an otherwise unknowable answer. Never mask concrete PoC-only integers, heap addresses,
ASLR values, allocator metadata, or incidental return codes unless that literal occurs
in relevant source. Keep them only in private evidence. Prefer relations between named
source variables/expressions: propagation, alias, size/capacity, before/after state,
branch conditions, order, and missing safety obligations.

Name trace fields after recoverable source expressions or explicit semantic views such
as `label_before`, `label_after`, and `free_argument`. Do not use opaque temporary names
whose relation to source code exists only in instrumentation. The root criterion's
`variable` must be the source expression represented by the required assertion operand.

The required assertion must recover the source-level conjunct that is absent from the
vulnerable program relative to its existing checks. Do not replace it with an equivalent
predicate over an instrumentation-only derived value; runtime evidence validates the
missing source obligation, not a newly invented formulation of it.

Derive the root obligation from the **sanitizer trace and the code**, not from a relation
that merely happens to hold at the sink, and not from `patch.diff`. The official fix
commit is unreliable for these samples — it is frequently an unrelated build, docs, or
version-bump commit that does not touch the vulnerable code at all — so `patch.diff` is at
most a corroborating hint and is **never** the authority. The trace is the authority:
- frame `#0` of the report is the **sink** (the unsafe operation);
- the sanitizer's origin section names the **root cause** directly. For MemorySanitizer,
  `Uninitialized value was created by an allocation of '<var>' in the stack frame of
  function '<fn>'` identifies the uninitialized object, and the obligation is that the
  bytes of `<var>` consumed at the sink are initialized on every reaching path. For
  AddressSanitizer, the `allocated by` (and, for use-after-free, `freed by`) stacks plus
  the reported out-of-bounds offset identify the object whose bounds or lifetime the
  missing check must enforce.

Identify the exact condition a correct program would enforce — a new guard, a
length/bounds test, a type/algorithm/format validation, an initialization, an ownership
or state check — and anchor the required assertion at the root-cause site the trace
points to (the allocation/declaration for an uninitialized-use, the length/bounds
computation for an overflow, the free/ownership point for a use-after-free), with the
fixed version as one operand of the before/after evidence. When the obligation is enforced
upstream of the sink (input rejected or corrected before the unsafe operation), it lives
at that upstream site: the vulnerable version violates it there (or reaches the unsafe
operation with it unmet) while the fixed version satisfies it, or per step 5 the fixed
original is `guarded`/`not_exercised` and needs a genuine-witness perturbation. Do not
phrase the obligation as a derived relation measured at the sink (for example
`buf_len >= read_len`) when it holds identically in the vulnerable and fixed runs: such an
obligation distinguishes nothing and cannot verify. If the vulnerable and fixed
measurements of a required assertion are equal, the obligation is anchored at the wrong
point — re-anchor it at the root cause the trace identifies.

Each transition must verify the semantic relation named by its invariant. For mutation,
compare the same source value immediately before and after the responsible operation.
For propagation or aliasing, compare the actual carried values (private pointer values
are valid runtime evidence), not two booleans or lengths that merely share a property.
A short-read relation does not by itself prove corruption, and equal nullness does not
prove that a corrupted pointer is the value passed to a sink.

## Perturbation utility rule

A final `clean` or `crash` result is not perturbation evidence by itself. When the fixed
original satisfies a required implication only because its protected event is absent,
the assertion remains unverified until at least one adjacent source-grounded case is
`genuine`: the required predicate is true and the protected event actually executes.
Prefer the closest valid boundary value. The assertion validator records that case as
`genuine_witness_case`; cases that merely produce `clean/clean` without changing
assertion execution status have no verification value.

Always ask the deterministic assertion validator to write `perturbation_results.json`.
It is a human-readable summary, not an independent oracle: it states whether perturbation
was necessary, why, every attempted case's assertion status, and which case supplied the
genuine witness. If perturbation was unnecessary it contains `needed: false` and no
invented cases.

## Minimal assertion schema

Top-level fields are only `schema_version`, `sample_id`, `original_case`,
`content_hash`, and `assertions`. Each assertion has `id`, `invariants`, `kind`, `at`,
`check`, and optional `protects`. `kind` is `observed`, `required`, or `transition`.
A transition additionally has `from`, and its check must directly relate one
`$from_event.field` to one `$at_event.field`; runtime order must be from before at.
Checks are `[op, left, right]` with `eq`, `ne`, `lt`, `le`, `gt`, or `ge`; `$field`
reads an `ASSERT_EVT` field. Do not duplicate prompts, answers, provenance, expected
matrices, or reciprocal assertion IDs.

## Probe placement (required)

A probe observes the statement it is placed at, not the block it sits in. Emit
each `ASSERT_EVT` immediately before the operation it is named for, after every
guard that can skip that operation. Do not group a block's probes together at
the top of the block: the fixed build usually differs from the vulnerable one by
an added guard that returns early, so probes hoisted above it fire on both sides
and observe nothing about whether the operation ran.

This matters most for the `protects` target. Its event is the evidence that the
dangerous operation actually executed, which is what makes `guarded` -- predicate
false, operation skipped -- the state a correct fix produces. A probe that fires
before the guard can never produce it, and the sample fails as though the patch
were wrong.

`gt_toolkit assertions` reports this as `differential_status: probe_misplaced`
with a `probe_placement_error` on the offending assertion, derived from a
contradiction rather than a guess: the protected operation cannot have run with
its safety obligation violated and still left the process clean. When you see it,
move the probe rather than weakening the assertion.

## Field bindings (required)

Every `$event.field` name you use must be a semantic view over a real, concrete
expression in the *vulnerable* original's own source -- the one you inserted (or
identified) an `ASSERT_EVT` marker next to to capture it. That real expression is
known at the moment you place the marker; it must not be discarded once verification
finishes. Write it to `<result_dir>/field_bindings.json`:

```json
{
  "schema_version": "field-bindings-v1",
  "sample_id": "<sample_id>",
  "bindings": {
    "<event>.<field>": "<exact vulnerable-original source expression, e.g. asn1_com_prkey_attr[0].parm>"
  }
}
```

One entry per distinct `$event.field` referenced anywhere in `assertions`. Use the
vulnerable original's expression -- that is the code the evaluated agent actually
reads, so its own account of the bug refers to those names. This file is consumed
downstream to recover what each `$event.field` really is in the source, so a
subject's description of the vulnerability can be matched against these assertions;
without it only the opaque semantic name (e.g. "free_argument") is available, which
no real source-level account would ever use.

## Event locations (required)

Every event-point ID you invent for the assertion graph (the `at`/`from`/`to`
values, e.g. `enqueue_deferred`, `cleanup_free`) is a synthetic node name --
it never exists as a real symbol in the project's source. To locate each
invariant in real code downstream (e.g. to check whether a subject's account of
the vulnerability reaches the right function), record each event ID's real,
locatable position. Write it to `<result_dir>/event_locations.json`:

```json
{
  "schema_version": "event-locations-v1",
  "sample_id": "<sample_id>",
  "locations": {
    "<event_id>": {"function": "<real function name>", "file": "<path relative to the vulnerable repo root>", "line": <int, for audit only>}
  }
}
```

One entry per distinct event ID referenced anywhere in `assertions`' `at`/`from`/`to`
fields, using the vulnerable original's function/file (the code the evaluated agent
reads). `line` is stored for audit/cross-checking only. Most event IDs coincide with
`ground_truth.json`'s own `root_cause`, `sink`, or one of the `fine_trace` steps --
reuse that file's `function`/`file`/`line` rather than re-deriving them.

Finally run:

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

PYTHONPATH=gt_generation python3 -m gt_toolkit assertions --check-bindings-only \
  --spec <result_dir>/candidate_assertions.json \
  --verified-invariants <result_dir>/verified_invariants.json
```

Use the reachability tool with the staged GT codebase and frozen assertion trace so
intermediate event points become debugger checkpoints, not merely sanitizer-stack guesses:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit reachability \
  --gt <result_dir>/ground_truth.json \
  --codebase <result_dir>/_work/src \
  --assertion-spec <result_dir>/candidate_assertions.json \
  --assertion-trace <result_dir>/vulnerable_assertion_trace.txt \
  --verified-invariants <result_dir>/verified_invariants.json \
  --sanitizer-trace <result_dir>/sanitizer_trace.txt \
  --debug-command '<debug binary and {poc}>' \
  --poc <result_dir>/poc \
  --out-dir <result_dir>/reachability
```

## Repo-track execution lifecycle

A non-ARVO sample has no `/bin/arvo` wrapper and no prebuilt tree. Everything --
the vulnerable build, the fixed build, the instrumented rebuilds, and the
debugger -- runs through `<result_dir>/build.sh`, which executes one shell
command inside `gt-memory-env` with the result directory bind-mounted at `/gt`.
Paths inside that command are therefore `/gt/...`, not host paths.

Do not run the target on the host. Repo-track binaries link against the image's
glibc and sanitizer runtime; the host has neither, so a host invocation fails
before `main` with a loader error rather than telling you anything about the
sample.

Reuse the exact command Stage 01 recorded in `reproduction_report.json` as
`command` -- it is already known to reproduce the crash against the vulnerable
build. For reachability, hand that command to the tool and let it run gdb inside
the same image:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit reachability \
  --gt <result_dir>/ground_truth.json \
  --codebase <result_dir>/_work/src \
  --assertion-spec <result_dir>/candidate_assertions.json \
  --assertion-trace <result_dir>/vulnerable_assertion_trace.txt \
  --verified-invariants <result_dir>/verified_invariants.json \
  --sanitizer-trace <result_dir>/sanitizer_trace.txt \
  --debug-command '<repro binary as /gt/... path> {poc}' \
  --debug-wrapper '<result_dir>/build.sh' \
  --debug-path-map '<result_dir>=/gt' \
  --poc /gt/poc \
  --out-dir <result_dir>/reachability
```

`--debug-wrapper` passes the whole gdb invocation to `build.sh` as one shell
word; `--debug-path-map` rewrites host paths under the result directory to their
`/gt` equivalents, including the gdb driver script the tool stages beside the
outputs. Omit both for ARVO, which runs the debugger directly.

`reachability_report.json` must end with `reachability_checked` true and the R
levels resolved. `audit-package` rejects a package whose reachability was never
executed, so a Stage 04 that skips this leaves the sample incomplete no matter
how good the assertions are.

## Re-runs

You may be started on a result directory where a previous attempt at this stage
already wrote `assertion_results.json`, `verified_assertions.json`, the
instrumentation patches and the traces. Those files are the record of an attempt
that failed. They are not evidence about this one, and an `evidence_limitation`
recorded in them is not a finding you can adopt -- it is the previous attempt's
account of where it stopped. Re-derive every artifact you are required to
produce; the runner only counts outputs written during this run.

Two things that a previous attempt may have concluded were unavailable are not:

- **The ARVO workspace rebuilds itself.** The container and images are removed
  after every run to keep disk bounded, so on a re-run there is no container and
  `docker ps` shows nothing. `apply-instrumentation` re-pulls the vulnerable
  image, recreates the container and runs the full vulnerable build before
  applying your patch. Start from the vulnerable side as usual; do not report a
  missing workspace as a blocker.
- **The `-fix` image is published for every ARVO sample.** If `switch-fixed`
  cannot apply `patch.diff`, or the patched build still crashes, run
  `compile-fixed --fallback-image`. A `differential_status` of
  `vulnerable_side_only` means that path was not taken, not that the fixed side
  is unobtainable.

If after actually executing both sides the differential still cannot be
established, say so with the commands you ran and their output.

## ARVO execution lifecycle

For an ARVO sample, reuse the configured workspace container and full vulnerable build
left by Stage 01. Generate vulnerable and fixed instrumentation as small git-apply
patches rooted at the project checkout recorded as `source_root` in
`<result_dir>/arvo_workspace.json`; the toolkit auto-detects that project git checkout
under `/src` (independent of `patch.diff`), so do not assume a project-specific
`/src/...` path. Then use:

Never edit tracked container source directly. Every instrumentation change must exist in
the persisted top-level `<result_dir>/vulnerable-instrumentation.patch` or
`<result_dir>/fixed-instrumentation.patch` passed to `apply-instrumentation`; temporary
paths are rejected. The workspace fingerprints the applied source and rejects
compile/run after any unpersisted edit.

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> apply-instrumentation \
  --patch <result_dir>/vulnerable-instrumentation.patch
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> compile-target --version vulnerable
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> run --version vulnerable --expect crash
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> switch-fixed --patch <result_dir>/patch.diff
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> apply-instrumentation \
  --patch <result_dir>/fixed-instrumentation.patch
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> compile-fixed
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> run --version fixed --expect clean
```

`switch-fixed --patch patch.diff` is **best-effort**: the official fix commit is often an
unrelated commit that does not apply or does not remove the crash. The authoritative
"the crash disappears in the real fixed program" oracle is the prebuilt **`-fix` image**,
not the patched source. If `switch-fixed` fails to apply, or the patched `run --version
fixed` still crashes, fall back to `compile-fixed --fallback-image` (which swaps to the
`n132/arvo:<id>-fix` image and rebuilds the instrumented target there) and treat that as
the fixed-side evidence. Do not fail the sample merely because `patch.diff` did not apply.

Stage 01's vulnerable compile is the only default full build. Stage 04's
`compile-target` and `compile-fixed` reuse that configured tree and rebuild only the
active fuzz target. If that Make target is unavailable, the toolkit synchronously falls
back to `/bin/arvo compile`; do not write polling loops or invoke a second compile
command.
PoC-only perturbations reuse the current binary and never rebuild. This stage never
selects or phrases evaluation questions. After the GT/assertion gates pass, clean only
this sample; the later probe generator consumes the persisted JSON and needs no container:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> cleanup --remove-images
```

## Non-ARVO execution lifecycle

For non-ARVO samples, keep every instrumented build and vulnerable/fixed execution in
the Docker environment recorded by `prepare_report.json`. Use
`<result_dir>/build.sh '<command>'`; it mounts the result directory at `/gt` and runs
from `/gt/_work/src`. Do not compile or execute an instrumented target directly on the
host. Apply and persist the vulnerable/fixed instrumentation patches in the mounted
checkout, and use the same configured image for both versions so the differential
evidence does not mix environments.
