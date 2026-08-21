# Role: Stage 04C Fixed Instrumentation

You are a fresh isolated coding-agent CLI session. Translate the already frozen
assertion plan into one minimal, read-only observation patch for the real fixed
source. Do not change any assertion, invariant, binding, event location,
vulnerable instrumentation, fine trace, or ground truth.

## Required output

- `fixed-instrumentation.patch`

Write artifacts with ordinary shell/Python file writes available inside the CLI
session. Do not invoke a special `apply_patch` command from the shell; it is not
part of the Stage 04C runtime environment.

Read `prepare_report.json` to choose the execution track. Read the frozen
semantic plan and inspect the true fixed source directly: for ARVO this is the
published `n132/arvo:<id>-fix` image; for repo-track samples this is the
repository after `build.sh` switches to `sample_info.fix_commit`. Do not apply
or infer fixed source from `patch.diff`; the fixed source selected by the
track-specific lifecycle is authoritative. Map the same semantic events to
their actual fixed-source locations. A guard may eliminate a protected
operation, but observations must still correspond to the frozen predicate and
must not invent a different invariant.

If `<result_dir>/instrumentation_feedback_fixed.md` exists, read it before
editing the patch. Treat it as the current deterministic validator mismatch:

- if it reports `apply_returncode` non-zero, regenerate
  `fixed-instrumentation.patch` against the exact fixed commit/tree selected by
  the preflight gate, not against a previously patched or vulnerable checkout;
- if it reports `compile_returncode` non-zero, fix only C/C++ syntax, missing
  includes, expression scope, or type availability in the observation patch;
- do not change `candidate_assertions.json`, `candidate_invariants.json`,
  `field_bindings.json`, `event_locations.json`, `ground_truth.json`, or
  `trace_feedback.json` to make the instrumentation gate pass.

For repo-track samples, the host source checkout starts at `<result_dir>/_work/src`
and the repo-workspace gate remounts it as `/gt/_work/src` before selecting the
fixed commit. Do not treat an OSS-Fuzz Dockerfile `WORKDIR` such as
`/gt/_work/<project>` as the host source path; use it only to understand the
build script lifecycle.

The patch must be observation-only:

- Before editing, enumerate the complete required event/field set from
  `candidate_assertions.json`: every assertion `at` event, every `transition`
  `from` event, and every `$event.field` operand appearing in any `check`.
  The patch must be able to print all of those events and all of those fields
  when the corresponding fixed-source locations are reached. A guarded fixed
  execution may legitimately skip a protected operation, but the patch must not
  omit source or propagation events that are still reachable and required by
  transition assertions.
- every runtime observation line must start with `ASSERT_EVT point=<event_id>`
  where `<event_id>` exactly matches the frozen assertion `at` or `from`
  event id;
- print every assertion operand field for that event as `field=<value>` using
  the same simple field name that appears after the dot in `$event.field`;
- do not print event ids as bare words such as `ASSERT_EVT root ...`; the
  deterministic parser only recognizes the event through the `point=` field;
- do not print source expressions as field names, for example avoid
  `ep->f_symtab_sect_strings=...`; use a stable field name such as `ptr=...`
  and let `field_bindings.json` map `root.ptr` to the source expression;
- keep field values parseable as single whitespace-delimited tokens, preferably
  integers, pointers, booleans, enum values, or `0`/`1` flags;
- for every non-literal field used by a `required` assertion, compute the value
  from real program state at that event. Do not print the expected answer as a
  constant. For example, if the plan checks
  `eq($root.free_before_use, $root.false_literal)`, `$root.false_literal` may
  print `0`, but `$root.free_before_use` must be computed from the observed
  lifetime/order state. The fixed original should satisfy the required safe
  predicate or avoid the protected operation through a real guard; do not make
  it appear satisfied by hard-coding the measured field.
- for uninitialized-value obligations, fields named `initialized_len`,
  `init_bytes`, `initialized`, or similar must measure bytes/objects actually
  written, copied, or zeroed before the protected read. Do not compute them from
  allocation capacity, requested size, or a variable updated by `realloc`/growth
  before the new region is initialized; the fixed side should satisfy the
  obligation because the fix writes/zeros the region or guards the use, not
  because the observation measures capacity.
- when emitting C/C++ string literals, escape newlines as `\\n` inside the
  source string. Never write a literal line break inside `"ASSERT_EVT ..."`
  because that produces an unterminated string and fails compilation;
- observe expressions valid in fixed-source scope;
- place operation events after guards and immediately before the operation;
- do not allocate, free, mutate state, add control flow, or change return
  values;
- when using `fprintf`, `stderr`, or another stdio symbol, add `#include
  <stdio.h>` to every touched C/C++ translation unit that does not already
  include it; another patched file's include does not provide the declaration;
- keep changes limited to project source, never the fuzz harness.

For ARVO, persist the patch and run the deterministic one-side compile gate:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> validate-instrumentation-side \
  --version fixed \
  --patch <result_dir>/fixed-instrumentation.patch \
  --out <result_dir>/fixed_instrumentation_preflight.json
```

Finish only when the report has `ok: true`. If application or compilation
fails, inspect `arvo_workspace/plan_fixed_apply.log` and
`arvo_workspace/plan_fixed_compile.log`, repair only this patch, and rerun the
gate. Never modify the frozen invariant or the vulnerable patch to hide a
fixed-side instrumentation error.

For repo-track samples (`prepare_report.track` starts with `repo/`), use the
repo-track gate instead:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit repo-workspace \
  validate-instrumentation-side \
  --result-dir <result_dir> \
  --version fixed \
  --patch <result_dir>/fixed-instrumentation.patch \
  --out <result_dir>/fixed_instrumentation_preflight.json
```

Finish only when that report has `ok: true`. If application or compilation
fails, inspect `repo_workspace/plan_fixed_apply.log` and
`repo_workspace/plan_fixed_compile.log`, repair only this patch, and rerun the
repo-track gate. Never modify the frozen invariant or the vulnerable patch to
hide a fixed-side instrumentation error.
