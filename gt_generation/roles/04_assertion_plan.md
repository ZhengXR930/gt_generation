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
   A required assertion is the safety-obligation predicate: it must be false in
   the vulnerable execution when the protected operation runs, and true in the
   fixed execution or in the single fixed perturbation after a guard. Do not use
   a predicate that the vulnerable runtime already satisfies, even if it names
   the right function or operands.
   Before writing the spec, make a two-column mental matrix for the proposed
   required predicate:
   `vulnerable original -> refuted` and `fixed original/one guarded
   perturbation -> satisfied`. If the vulnerable trace already satisfies the
   predicate, or if both sides are expected to have the same truth value, the
   assertion is not a root obligation and must be redesigned.
4. Record every assertion operand in `field_bindings.json` as an exact
   vulnerable-source expression. Record every synthetic event in
   `event_locations.json` with its real vulnerable-source function, file, and
   line.
5. Write `candidate_invariants.json` directly in the GT contract graph shape:
   no artifact-level `schema_version`; `root_cause_criterion` is only
   `{"invariant_id": "<root node id>"}` and that id must point to a real
   `nodes[]` entry with `role: "root_cause"`. Every node and every edge must
   have `operands` as source-expression strings and `relation` as
   `{ "op": "...", "left": "...", "right": "..." }`. Every edge must have
   `from_node` and `to_node` pointing at node `invariant_id`s. `type` is
   optional free text, not a required schema field.
6. Stop at the semantic commitment. Do not create instrumentation patches,
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

Use the verifier's operand grammar literally:

```json
{"kind": "observed", "check": ["gt", "$sink.str_size", "$root.off"]}
{"kind": "transition", "from": "source", "at": "sink",
 "check": ["eq", "$source.ptr", "$sink.ptr"]}
```

The following shapes are invalid:

```json
{"kind": "observed", "check": ["gt", "$sink.str_size", 1000000]}
{"kind": "transition", "from": "source", "at": "sink",
 "check": ["eq", "$source.ptr", 0]}
```

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
