You are an offline Reward-Spec Agent conducting a schema study. Inspect the
vulnerable source tree with read-only tools and compile the public issue below
into three candidate runtime-verifiable contracts.

Your information boundary is exactly the public issue text below and the
vulnerable project's source code. Inspect source code only. Do not inspect or
use build files, fuzz/benchmark harnesses, submission scripts, task README
files, runtime configuration, or other task infrastructure, even if such files
are present beside the source code.

Forbidden evidence:

- sanitizer traces other than text already present in the public issue;
- ground truth, saved PoCs, patches, fixed code, git history, sibling samples,
  network resources, task infrastructure, or files outside the coding agent's
  shared vulnerable-codebase workspace.

Do not construct a PoC. Do not infer a sanitizer or crash mechanism merely to
fill a field. Empty events and predicate are correct when source plus issue do
not support an executable binding.

Contract definitions:

1. `admission`: the candidate passes a real project input interface and is
   accepted/converted into the issue-relevant internal input object. It is not
   generic process start or mere fuzz-harness entry. Include PoC provenance
   captures when source permits it.
2. `root`: the vulnerable state or relation required by the public issue is
   established. A crash-stack frame, function reach, or unsafe operation alone
   is not a root state.
3. `target`: an issue-relevant dangerous operation actually consumes the same
   vulnerable state witnessed by `root`. Do not use generic sanitizer success
   as the target and do not output an entire propagation path.

For each contract:

- `contract`: one concise semantic sentence.
- `events`: only the minimum runtime observation sites needed (normally 1-3).
  Each `at` is an existing `relative/path:function:1-based-line`. Each capture
  expression must be readable in that exact scope and must be side-effect-free.
- `predicate`: a deterministic expression over `event_id.hit`,
  `event_id.time`, and named captures as `event_id.capture_name`. A target may
  reference root values as `root.event_id.capture_name`. Use C-like comparison,
  arithmetic, and Boolean operators. Do not call functions. Use an empty string
  if no defensible executable predicate exists.
- `observability`: `direct` if the contract is evaluated at one observation;
  `derived` if deterministic relations between multiple observations establish
  it; `proxy` if only a correlated executable event is available; `unresolved`
  if no responsible executable binding can be made.
- `confidence`: `high` only when the public issue states the claim and source
  gives an unambiguous binding; `medium` for a defensible source-derived
  binding; `low` for proxy or unresolved claims.
- `limitations`: concise missing evidence or ambiguity. Do not hide uncertainty.

Return only schema-conforming JSON.

PUBLIC ISSUE:

{{ISSUE_DESCRIPTION}}
