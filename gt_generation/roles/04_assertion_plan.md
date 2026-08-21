# Role: Stage 04A Assertion Plan

You are a fresh isolated coding-agent CLI session. Enter only when
`static_review.json` has all four review booleans true. Convert the accepted
fine trace into a minimal, source-derived assertion plan. Do not execute the
target, run reachability, rewrite the GT, or inspect prior runtime assertion
results.
If `<result_dir>/assertion_plan_feedback.md` exists, read it before planning:
it records a human-confirmed diagnosis of why the previous frozen assertion was
not a valid vulnerable/fixed differential. Treat that file as a constraint on
what not to repeat, not as runtime evidence to copy.
If that feedback says the vulnerable original already satisfied the required
predicate, the previous predicate was not the missing safety obligation. Do not
keep the same `check` with renamed fields, and do not move only the probe. Pick
a different source-level safety condition that is false on the crashing
vulnerable witness and true, guarded, or avoided on the fixed side.

## Required outputs

- `candidate_assertions.json`
- `candidate_invariants.json`
- `field_bindings.json`
- `event_locations.json`
- `.assertion_spec_frozen.json`
- `assertion_preflight.json`

`sample_info.json`, `build.sh`, `poc`, `patch.diff`, and `ground_truth.json`
are immutable inputs.

Do not inspect, grep, copy, or adapt assertion/invariant artifacts from other
samples under `gt_results/` or backup directories. Cross-sample templates often
encode a different vulnerability mechanism and contaminate the current GT. The
only allowed examples are the schema snippets in this role file and deterministic
tool error messages from the current `<result_dir>`.

For repo-track samples, the vulnerable source checkout on the host is
`<result_dir>/_work/src`; the same tree is mounted inside repo-workspace
containers as `/gt/_work/src`. Use `prepare_report.json` for track selection and
project metadata, but do not infer a host source path from an OSS-Fuzz Dockerfile
`WORKDIR` such as `/gt/_work/<project>`. That workdir is a build-time alias; the
host source tree to inspect for this stage is `<result_dir>/_work/src`.

Write artifacts with ordinary shell/Python file writes available inside the CLI
session. Do not invoke a special `apply_patch` command from the shell; it is not
part of the Stage 04A runtime environment.

## Work Order

Follow these steps in order. Do not skip ahead to writing JSON before the root
obligation and event fields pass the checks below.

1. Read the accepted GT and source context.
   - Read `ground_truth.json`, `sanitizer_trace.txt`, `reproduction_report.json`,
     `sample_info.json`, `prepare_report.json`, and the vulnerable source files
     named by the GT source/root/sink anchors.
   - Do not read sibling result directories, previous successful samples, or
     `gt_results/*/candidate_assertions.json` / `candidate_invariants.json` as
     examples. If you need schema shape, use the snippets in this prompt only.
   - If `assertion_plan_feedback.md` exists, first list the prior failed predicate
     or probe placement in your private reasoning and make sure the new plan does
     not repeat it.
   - Do not use `patch.diff` as the source of truth for the invariant. The fixed
     source will be checked later by the fixed instrumentation stage.

2. Select the minimal invariant graph.
   - Create one `source` node for the first project-code materialization of the
     attacker-controlled value or state.
   - Create one `root_cause` node for the missing safety obligation: the predicate
     that should be true before the dangerous operation, but is false for the
     vulnerable crashing witness.
   - Create one `sink` node for the project-code unsafe operation that consumes the
     bad pointer, length, index, object, lifetime state, or C string.
   - Add `intermediate` nodes only when a source-level value, alias, owner, lifetime
     state, size, or control decision changes and that change is needed to explain
     the next scored node.
   - Exclude ordinary reachability plumbing, duplicate observations, incidental PoC
     constants, generic API facts, and fuzz harness callbacks/helpers.

3. Design the root obligation before any other assertion.
   - Write the root predicate as the safe condition that protects the sink.
   - Choose that predicate from the vulnerability mechanism, not from the first
     fixed-side guard-looking condition near the trace. A valid root obligation
     is the safety property whose absence makes the sink unsafe for the accepted
     crashing witness.
   - The predicate must be a concrete comparison over source-level values, not a
     prose label. Use `relation.op` only from the GT contract set:
     `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `contains`, `same_object`,
     `free_before_use`, `use_before_init`, `use_after_return`,
     `use_after_scope`, or `double_free`. Put phrases such as
     `missing_validation`, `read_past_end`, `deref_after_end`,
     `not_checked_before_use`, and other English explanations in
     `description`, then express the actual measurable condition through
     `relation.left` / `relation.right`.
   - In `candidate_assertions.json`, `check[0]` is stricter: it must be one of
     `eq`, `ne`, `lt`, `le`, `gt`, or `ge` because the runtime verifier executes
     it on captured scalar fields.
   - Every `required` assertion must include a `mechanism` field with exactly one
     of: `bounds`, `lifetime`, `initialization`, `string_termination`,
     `invalid_free`, or `other`. Do not omit it; deterministic preflight uses
     this field for mechanism-specific rejection of known-bad root shapes.
   - It must be expected to be false in the vulnerable original execution when the
     protected operation runs.
   - It must be expected to be true in the fixed original execution, or the fixed
     original must skip the protected operation through a guard. A single fixed
     perturbation is allowed later only for this guarded/avoided case.
   - If the candidate predicate would be true in the vulnerable crashing execution,
     it is not the root obligation. Pick a different predicate.
   - For each candidate root predicate, explicitly reject it before writing JSON
     when it is merely a routing/dispatch condition, block-completion condition,
     buffer-capacity fact, or identity fact that can be true on both vulnerable
     and fixed executions. Those facts can be `source`, `sink`, or propagation
     evidence, but they are not the missing safety obligation.

4. Match the root-obligation shape to the bug mechanism.
   - Use-after-free, double-free, use-after-return, or dangling-alias bugs:
     make the obligation a lifetime predicate at the last ownership/lifetime
     decision before the protected use. Prefer an executable scalar such as
     `eq($root.alive, $root.true_literal)`,
     `eq($root.owner_released, $root.false_literal)`,
     or a directly measured ordering predicate where the free/release event
     must not precede the protected use. Do not use pointer inequality such as
     `ptr != owner_base` as the root obligation: interior pointers are normally
     not equal to their allocation base even when they are valid. Do not use an
     unrelated size, capacity, or block-completion predicate just because the fix
     adds or rearranges such a guard. If the vulnerable witness reads through an
     alias after its owner is freed, the root obligation is about the owner/alias
     still being alive at the protected use, or about `free_before_use` being
     false, not about the surrounding block being complete. Do not place a
     lifetime root inside cleanup/finalizer functions such as `free*`,
     `destroy*`, `deinit*`, `cleanup*`, `release*`, `close*`, or `finalize*`
     just to assert that a pointer/owner/buffer is `NULL`, `0`, or `false`;
     that is a post-cleanup state, not the pre-use safety obligation that
     protects the sink.
     Boolean or ordering fields must use their literal runtime meaning. If a
     field is named `free_before_use`, it must print `1` exactly when the
     release/free event happened before the protected use and `0` otherwise; do
     not define it as the value that would make the safe predicate pass.
   - Uninitialized-value bugs: make the obligation about initialization coverage
     before the protected read or compare, for example
     `ge($root.initialized_len, $root.read_len)` or
     `eq($root.initialized, $root.true_literal)`. Do not use total buffer
     capacity if the read length is within capacity but contains poison.
     Names such as `initialized_len`, `init_bytes`, or `initialized` must mean
     bytes or objects actually written, copied, or zeroed before the protected
     read. They must not mean current allocation size, current capacity, or the
     resize target after `realloc`/growth has already updated the size variable.
     If the bug is an uninitialized tail after growth, bind the left side to the
     old initialized prefix or to an explicit "zeroed/written" flag, not to the
     new capacity.
     For MemorySanitizer witnesses, the left side must be the defined/initialized
     prefix that reaches the protected read, and the right side must be the read
     width or consumed extent. If both sides are the requested sample count,
     allocation size, current capacity, or post-growth size, the predicate will
     be true in the vulnerable run and is invalid.
   - Out-of-bounds read/write bugs: make the obligation the exact access bound,
     for example `le($root.idx_plus_width, $root.len)`,
     `lt($root.idx, $root.count)`, or `le($root.copy_len, $root.dst_len)`.
     A weaker condition that is still true at the crashing access is invalid.
   - C-string/NUL-termination bugs: make the obligation about a terminator or
     bounded remaining length before the unbounded string operation, not merely
     pointer-in-allocation.
   - Invalid-free bugs: make the obligation about allocation ownership or valid
     free preconditions before `free`/destroy, not about reachability of the
     cleanup path.

5. Fill this truth table mentally before writing `candidate_assertions.json`:

   ```text
   required assertion id:
   mechanism class: bounds | lifetime | initialization | string_termination | invalid_free | other
   protected operation:
   vulnerable original: violated because <source-level values>
   fixed original: satisfied OR guarded/avoided because <source-level guard>
   one fixed perturbation needed: yes/no
   ```

   If any cell is unknown from the source and saved Stage 01 evidence, simplify the
   graph or choose a different assertion. Do not invent a numeric threshold or
   runtime field to make the table pass.

6. Choose event ids and runtime fields.
   - Use short event ids such as `source`, `root`, `sink`, `dispatch`,
     `bounds_check`, or `cursor_advance`.
   - For each event, choose fields that can be printed at that exact source line.
     Field names must be simple identifiers: `ptr`, `len`, `limit`, `off`, `idx`,
     `size`, `flag`, `state`, `ch`, `base`.
   - The event line must be after the asserted value exists. If the required
     predicate is `eq($root.field, NULL)`, `eq($root.field, 0)`, or
     `eq($root.field, false)`, do not place `root` before the assignment that
     sets that field to the sentinel; that asserts a future cleanup/update, not
     the current safety obligation.
   - The field suffix in `$event.field` is the exact name later printed by
     instrumentation. If the assertion uses `$root.len`, the trace must later contain
     `ASSERT_EVT point=root len=<value>`.
   - Bind each field to an exact vulnerable-source expression in
     `field_bindings.json`.
   - Do not use source expressions or prose labels as field names.

7. Place protected events precisely.
   - `protects` must be an event id present in `event_locations.json`, usually
     `sink`. It must never be a source expression, struct field, operation name,
     prose phrase, or variable such as `header->numbodyparts`.
   - `protects` must name the dangerous operation event, not a surrounding block or
     earlier guard.
   - The future instrumentation must be able to print the protected event immediately
     before the dangerous read/write/free/call and after every guard that can skip it.
   - If the fixed source adds a guard before the operation, the fixed original may
     avoid the protected event. That is acceptable only when the required predicate is
     truly violated in vulnerable and the guard explains why fixed is clean.

8. Add propagation assertions only after the root is valid.
   - Every selected edge gets exactly one `transition` assertion with `from`, `at`,
     and a field-to-field check.
   - Propagation checks must directly relate the carried value at the two endpoint
     events: pointer identity, length equality, index derivation, control predicate,
     or lifetime ordering.
   - If no direct source-derived field relation can be measured, omit that edge from
     the candidate graph. Do not weaken the root obligation to force propagation
     coverage.

9. Add observed assertions only for vulnerable-side facts.
   - Observed assertions must hold in the vulnerable execution and must compare two
     captured event fields.
   - Do not compare an observed assertion to an inline literal, heap address, or PoC
     byte count. If the fact needs a constant, either it belongs in a required safety
     obligation or it should not be an observed assertion.

10. Write the four required plan artifacts.
   - `candidate_assertions.json`: assertion-spec-v3 plus canonical `content_hash`.
     Do not hand-compute or guess this field. Write the assertion spec with a
     placeholder or omit the field, then run the freeze command below; it rewrites
     `content_hash` to the required `sha256:<64 hex>` canonical value.
   - Every assertion object must include a non-empty `invariants` array. Each id in
     that array must be an `invariant_id` from `candidate_invariants.json` nodes or
     edges. The required root assertion must reference the root-cause node id, for
     example `["N_ROOT"]`; transition assertions must reference the edge id they
     prove, for example `["E_SOURCE_TO_ROOT"]`; observed sink assertions must
     reference the sink node id, for example `["N_SINK"]`.
   - `candidate_invariants.json`: the GT contract graph; no artifact-level
     `schema_version`.
   - `field_bindings.json`: every `$event.field` mapped to exact source expression
     and aliases.
   - `event_locations.json`: every event mapped to real vulnerable-source
     file/function/line.
   - For every root_cause and sink node, keep `operands` and
     `relation.left/right` aligned with the assertion fields. If the runtime
     assertion proves `ge($root.available, $root.required)`, the invariant
     relation should be `{"op":"ge","left":"available_expr","right":"required_expr"}`.
     Do not put a full declaration, statement, sentence, or issue summary into
     an operand.

11. Freeze and preflight.
    - Run the freeze command.
    - Run assertion preflight.
    - Finish only when `assertion_preflight.json` has `ok: true`.
    - Do not create instrumentation patches, run the target, write verified
      assertions, or write verified invariants. Later stages own that work.

The required assertion must be recoverable from the sanitizer trace and source,
not from `patch.diff`, heap addresses, allocator metadata, or PoC-only constants.
Do not read `patch.diff` to construct, select, or reject an invariant. Stage 01
already established the vulnerable/fixed PoC differential; this stage explains
the vulnerable execution using the accepted fine trace, sanitizer trace, and
vulnerable source.
For a guarded fix, `protects` names the dangerous operation whose absence will
be distinguished during execution.

## Assertion semantics hard rules

These rules are structural, not stylistic. Violating one of them produces an
unusable GT package.

1. `required` is the only vulnerable/fixed differential assertion. It must be a
   source-level safety obligation that the vulnerable original violates and the
   fixed side satisfies or guards. It is not an observed bad-state fact and not
   a restatement that execution reached the right function.
2. If the accepted vulnerable trace shows the proposed required predicate is
   already true, reject it. Example: if runtime events show `throw_flag=0`,
   then `eq($sink.throw_flag, $sink.false_literal)` cannot be the missing root
   obligation for that witness.
3. `observed` assertions describe facts that hold in the real vulnerable
   execution. Both operands after the operator must be `$event.field` strings.
   Do not compare an observed assertion to an inline literal such as `1000000`,
   `0`, `NULL`, or a heap address. If the semantic fact is "huge remaining
   length", express it as a source-derived relation such as
   `gt($sink.str_size, $root.off)` and bind both fields.
4. `transition` assertions verify propagation between two events. They must
   have `from`, `at`, and a check that directly relates one `$from_event.field`
   to one `$at_event.field`. Do not use constants, PoC byte counts, allocator
   addresses, or unrelated fields.
5. Required assertions may use literals only when the literal is a source-level
   safety value: a real source literal, macro, enum, sentinel, NULL/0 boolean,
   or a consciously chosen wrap/bounds threshold justified in the node
   description and field bindings. Never invent a numeric threshold merely to
   make an observed assertion pass.
6. If no source-derived field-to-field observed/transition relation can be
   written for a candidate node or edge, omit that node/edge from the candidate
   graph. Keep the root required differential correct instead of forcing
   propagation coverage.
7. If the trace review selected the wrong root/sink relation, rewrite the GT
   semantics in `candidate_invariants.json` to the relation actually proven by
   source and runtime. Do not preserve a wrong `op/left/right` just because it
   appeared in an earlier draft.

## Common wrong plans to avoid

- C-string/NUL bugs: do not use `ptr <= limit` as the root obligation merely
  because the pointer is still inside the allocation at the start of `strlen`,
  `strchr`, or a character decoder. The safety obligation is about a terminator
  or enough remaining bytes before the unbounded/required read. Express it with
  source-derived fields such as `nul_found`, `remaining`, `required`, `off`,
  and `len`.
- Cursor lookahead bugs: do not place the protected event at the top of an `if`
  body before the fixed guard can skip the dangerous lookup. Put it immediately
  before the dereference, table lookup, `CUR`, `NXT`, `strlen`, `memcpy`,
  indexed read/write, `free`, or call that consumes the unsafe value.
- Bounds bugs: do not write a required predicate that is true in the vulnerable
  crash, such as `off < len` when the crash is caused by a later multi-byte read.
  Use the actual precondition: for example `off + needed <= len`,
  `field_end <= buffer_len`, or `copy_len <= dst_len`.
- C++ layout checks: avoid instrumentation expressions such as
  `offsetof(NonStandardLayoutType, field)` when the project builds with
  `-Werror` or the type may be non-standard-layout. Prefer source variables that
  already hold the needed size/limit, or simple runtime values that can be
  printed without compiler diagnostics.
- Identity facts: `eq(x, x)` and `same_object(x, x)` are not root obligations.
  They are source or propagation observations only.
- Natural-language relation ops such as `missing_validation`, `read_past_end`,
  `deref_after_end`, or `not_checked_before_use` are not executable
  comparisons. Preserve that wording in `description`; set `relation` to the
  real source-level comparison that the assertion will verify.

Use the verifier's operand grammar literally:

```json
{"kind": "observed", "check": ["gt", "$sink.str_size", "$root.off"]}
{"kind": "transition", "from": "source", "at": "sink",
 "check": ["eq", "$source.ptr", "$sink.ptr"]}
```

Runtime assertions do not resolve abstract fields through `field_bindings.json`.
They evaluate the exact field names captured in `ASSERT_EVT` records. Therefore
the field suffixes in every `$event.field` operand must exactly match the
instrumentation output fields for that event. `field_bindings.json` is a source
audit map for those same operand names, not a runtime alias layer.

The following shapes are invalid:

```json
{"kind": "observed", "check": ["gt", "$sink.str_size", 1000000]}
{"kind": "transition", "from": "source", "at": "sink",
 "check": ["eq", "$source.ptr", 0]}
{"kind": "required", "at": "root", "mechanism": "lifetime",
 "check": ["eq", "$root.buffer_owner_field", "$root.alias_owner_field"]}
```

The last shape is invalid when instrumentation would print source-expression
labels such as `ep_ptr` or `gh_content` instead of fields literally named
`buffer_owner_field` and `alias_owner_field`.

## GT Contract Outputs

Do not write artifact-level `schema_version` in `candidate_invariants.json`,
`field_bindings.json`, or `event_locations.json`. The assertion spec itself
still uses `schema_version: "assertion-spec-v3"` because the assertion freeze
hash needs a stable protocol marker.

`field_bindings.json` binding values must use the alias-capable object form:

```json
{
  "sample_id": "<sample_id>",
  "bindings": {
    "<event>.<field>": {
      "expr": "<exact vulnerable-original source expression>",
      "aliases": ["<same expression>", "<macro or spelling alias if applicable>"]
    }
  }
}
```

Each `expr` and each alias used by an assertion must be either an exact
vulnerable-source expression, a compile-time literal/macro spelling, or a short
instrumentation scalar name that the instrumentation stage can compute at the
event. Do not put English witness descriptions in `expr` or `aliases`, such as
`free executes before the later read`, `recorded by ASan`, `for this witness`,
or `uses X as its pointer`. Put that text in `description` or a top-level note.
For an ordering obligation, bind the runtime field to a computable scalar name
such as `free_before_use`, and let the instrumentation stage compute `0` or `1`
from concrete event ordering.

`event_locations.json` is:

```json
{
  "sample_id": "<sample_id>",
  "locations": {
    "<event_id>": {"function": "<real function>", "file": "<repo-relative file>", "line": <int>}
  }
}
```

`candidate_invariants.json` is:

```json
{
  "sample_id": "<sample_id>",
  "nodes": [
    {
      "invariant_id": "N_SOURCE",
      "role": "source",
      "file": "...",
      "function": "...",
      "line": 1,
      "operands": ["source_expr"],
      "relation": {"op": "same_object", "left": "source_expr", "right": "source_expr"},
      "verified": true
    },
    {
      "invariant_id": "N_ROOT",
      "role": "root_cause",
      "file": "...",
      "function": "...",
      "line": 2,
      "operands": ["lhs", "rhs"],
      "relation": {"op": "lt", "left": "lhs", "right": "rhs"},
      "verified": true
    },
    {
      "invariant_id": "N_SINK",
      "role": "sink",
      "file": "...",
      "function": "...",
      "line": 3,
      "operands": ["sink_expr", "bound_expr"],
      "relation": {"op": "ge", "left": "sink_expr", "right": "bound_expr"},
      "verified": true
    }
  ],
  "edges": [
    {
      "invariant_id": "E_ROOT_TO_SINK",
      "type": "data",
      "from_node": "N_ROOT",
      "to_node": "N_SINK",
      "operands": ["carried_expr"],
      "relation": {"op": "eq", "left": "root_expr", "right": "sink_expr"},
      "verified": true
    }
  ],
  "root_cause_criterion": {"invariant_id": "N_ROOT"}
}
```

## Freeze

Write a complete `assertion-spec-v3`; you may omit `content_hash` or set a
placeholder because the freeze command below is authoritative and rewrites it:

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit assertions \
  --spec <result_dir>/candidate_assertions.json \
  --freeze-only \
  --freeze-marker <result_dir>/.assertion_spec_frozen.json
```

The freeze command is authoritative for the hash. If it changes
`candidate_assertions.json`, use that rewritten file for every later preflight
and instrumentation step.

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
