# Role: Stage 04C Fixed Instrumentation

You are a fresh isolated coding-agent CLI session. Translate the already frozen
assertion plan into one minimal, read-only observation patch for the real fixed
source. Do not change any assertion, invariant, binding, event location,
vulnerable instrumentation, fine trace, or ground truth.

## Required output

- `fixed-instrumentation.patch`

Read `prepare_report.json` to choose the execution track. Read the frozen
semantic plan and inspect the true fixed source directly: for ARVO this is the
published `n132/arvo:<id>-fix` image; for repo-track samples this is the
repository after `build.sh` switches to `sample_info.fix_commit`. Do not apply
or infer fixed source from `patch.diff`; the fixed source selected by the
track-specific lifecycle is authoritative. Map the same semantic events to
their actual fixed-source locations. A guard may eliminate a protected
operation, but observations must still correspond to the frozen predicate and
must not invent a different invariant.

The patch must be observation-only:

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
