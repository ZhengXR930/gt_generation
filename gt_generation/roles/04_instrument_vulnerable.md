# Role: Stage 04B Vulnerable Instrumentation

You are a fresh isolated coding-agent CLI session. Translate the frozen
assertion plan into one minimal, read-only observation patch for the real
vulnerable source. Do not change any assertion, invariant, binding, event
location, fine trace, or ground truth.

## Required output

- `vulnerable-instrumentation.patch`

Write artifacts with ordinary shell/Python file writes available inside the CLI
session. Do not invoke a special `apply_patch` command from the shell; it is not
part of the Stage 04B runtime environment.

Read `candidate_assertions.json`, `candidate_invariants.json`,
`field_bindings.json`, `event_locations.json`, `sanitizer_trace.txt`, and the
vulnerable source. Do not use `patch.diff` to choose events or construct the
patch.

If `<result_dir>/instrumentation_feedback_vulnerable.md` exists, read it before
editing the patch. Treat it as the current deterministic validator mismatch:

- if it reports `apply_returncode` non-zero, regenerate
  `vulnerable-instrumentation.patch` against the exact vulnerable commit/tree
  selected by the preflight gate, not against a previously patched or fixed
  checkout;
- if it reports `compile_returncode` non-zero, fix only C/C++ syntax, missing
  includes, expression scope, or type availability in the observation patch;
- do not change `candidate_assertions.json`, `candidate_invariants.json`,
  `field_bindings.json`, `event_locations.json`, `ground_truth.json`, or
  `trace_feedback.json` to make the instrumentation gate pass.

For repo-track samples, inspect the host vulnerable checkout at
`<result_dir>/_work/src`; repo-workspace gates mount that same tree as
`/gt/_work/src`. Do not treat an OSS-Fuzz Dockerfile `WORKDIR` such as
`/gt/_work/<project>` as the host source path. Use those Dockerfile workdirs only
when reasoning about the build script lifecycle.

Insert only the `ASSERT_EVT` observations required by the frozen plan:

- Before editing, enumerate the complete required event/field set from
  `candidate_assertions.json`: every assertion `at` event, every `transition`
  `from` event, and every `$event.field` operand appearing in any `check`.
  The patch must be able to print all of those events and all of those fields
  on the vulnerable execution when their source locations are reached. Do not
  omit a source event just because the root/sink event is enough for the
  required assertion; transition assertions need their `from` events too.
- every runtime observation line must start with `ASSERT_EVT point=<event_id>`
  where `<event_id>` exactly matches the assertion `at` or `from` event id;
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
  lifetime/order state and must be `1` on a use-after-free witness that violates
  the obligation. The vulnerable original must refute the required safe
  predicate when the protected operation runs.
- for uninitialized-value obligations, fields named `initialized_len`,
  `init_bytes`, `initialized`, or similar must measure bytes/objects actually
  written, copied, or zeroed before the protected read. Do not compute them from
  allocation capacity, requested size, or a variable updated by `realloc`/growth
  before the new region is initialized; that would make the vulnerable run look
  safe while the sanitizer still reports uninitialized data.
- when emitting C/C++ string literals, escape newlines as `\\n` inside the
  source string. Never write a literal line break inside `"ASSERT_EVT ..."`
  because that produces an unterminated string and fails compilation;
- observe expressions already valid at that source location;
- place a protected-operation event immediately before the operation and after
  every guard that may skip it;
- do not allocate, free, mutate program state, add branches, or change return
  values;
- use declarations and output facilities valid in that translation unit;
- when using `fprintf`, `stderr`, or another stdio symbol, add `#include
  <stdio.h>` to every touched C/C++ translation unit that does not already
  include it; another patched file's include does not provide the declaration;
- keep the patch minimal and portable.

Read `prepare_report.json` to choose the execution track. For ARVO, persist
the patch and run the deterministic one-side compile gate:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> validate-instrumentation-side \
  --version vulnerable \
  --patch <result_dir>/vulnerable-instrumentation.patch \
  --out <result_dir>/vulnerable_instrumentation_preflight.json
```

Finish only when the report has `ok: true`. If application or compilation
fails, inspect `arvo_workspace/plan_vulnerable_apply.log` and
`arvo_workspace/plan_vulnerable_compile.log`, repair only this patch, and rerun
the gate. Never rewrite the frozen semantic plan to accommodate an
instrumentation mistake.

For repo-track samples (`prepare_report.track` starts with `repo/`), use the
repo-track gate instead:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit repo-workspace \
  validate-instrumentation-side \
  --result-dir <result_dir> \
  --version vulnerable \
  --patch <result_dir>/vulnerable-instrumentation.patch \
  --out <result_dir>/vulnerable_instrumentation_preflight.json
```

Finish only when that report has `ok: true`. If application or compilation
fails, inspect `repo_workspace/plan_vulnerable_apply.log` and
`repo_workspace/plan_vulnerable_compile.log`, repair only this patch, and rerun
the repo-track gate.
