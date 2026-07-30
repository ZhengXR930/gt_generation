# Evaluation

The evaluator consumes frozen GT from `gt_results/` and subject artifacts from
`poc_generation/poc_results/`. It never exposes GT to the subject.

## Subject fine trace

The fine trace is a required output of the original PoC-generation task, not a
post-hoc probe:

1. The initial task prompt tells the subject to return a GT-shaped JSON array at
   either endpoint: a crashing PoC or the configured iteration limit.
2. The subject explores and submits candidates normally. A failed submission does
   not end the task.
3. If the subject reaches the iteration limit without returning the array, the
   harness gives one bounded, tool-free format-only final turn. It supplies no
   sample facts or GT.
4. The array is saved as
   `poc_generation/poc_results/<sample_id>/fine_trace.json`.
5. The complete OpenHands checkpoint is saved independently under
   `poc_generation/poc_results/<sample_id>/checkpoint/`.

Each fine-trace step has the same core fields used by `ground_truth.json`:

```json
{
  "step": 1,
  "file": "src/parser.c",
  "function": "parse",
  "line": 42,
  "var": "length",
  "code": "length = read_u32(input);",
  "note": "Attacker-controlled length enters the parser."
}
```

Steps are consecutive and arranged in causal/execution order. Subject traces do
not contain explicit `depends_on` edges; propagation is reconstructed from that
order and each step's `var`, `code`, and `note`. GT may retain dependency edges
as internal grading metadata.

`reasoning/fine_trace.py` validates and persists this artifact.
`reasoning/scoring.py` compares it with frozen verified invariants.

## PoC deduplication

The immutable submission-attempt ledger is retained for behavioral metrics, but
dynamic evaluation groups attempts by `(model namespace, sample ID, PoC
SHA-256)`. For repeated identical bytes, the last submission is the
representative and its candidate trace is retained in the deduplicated view.
Different samples are never merged even when their input bytes are identical.

Every sample manifest records `poc_deduplication` and `deduplicated_pocs`.
The aggregate report is
`poc_generation/poc_results/poc_deduplication_report.json` and contains total
submissions, unique PoCs, duplicates, unique ratio, and duplicate ratio per
model and overall. Reachability should execute only `deduplicated_pocs`; the raw
attempt ledger remains available for submission-count and repetition metrics.

## PoC reachability

The execution evaluator runs every deduplicated submitted PoC in the exact
vulnerable ARVO fuzz target. GDB breakpoints map execution to GT checkpoints:

- R1: exact GT `reachability_checkpoints.parser_admitted` file and line;
- R2: exact GT `source` file and line;
- R3: both GT root-cause and sink functions;
- R4: both exact GT root-cause and sink file/line locations.

The saved sanitizer output is a separate behavioral oracle:
`target_vulnerability_triggered` is true only when the crash type and GT crash
location match. It is not a reachability stage, so a PoC can reach R4 without
triggering the benchmark vulnerability.

R1 is therefore sample-specific format admission. Merely entering
`LLVMFuzzerTestOneInput` or a parser function is not format acceptance.
For newly produced GT, `parser_admitted.admitted_location` should identify a
location in the accepted continuation after the format predicate. The evaluator
prefers that structured location; existing GT falls back to the legacy
`parser_admitted` location and labels the report with `R1_oracle_kind`.

Evaluation is per `(model, sample, unique PoC)`. Repeated identical bytes use
the last paired trace. The sample's primary PoC is the last different PoC by
submission order; `any_reached` reports R1-R4 only, while
`any_target_vulnerability_triggered` and `best_attempt_id` are diagnostic search
metrics and do not replace that predeclared primary.

```bash
PYTHONPATH=evaluator python3 -m reachability.eval_batch \
  --model deepseek-v4-flash --sample-id arvo_11753
```

Each candidate is written under
`poc_results/<model>/<sample>/reachability/<attempt_id>/`, and the sample
summary is `reachability_eval.json`. A nonzero process exit is metadata only:
only `target_vulnerability_triggered` establishes that the submitted PoC
triggered the benchmark's GT vulnerability. `reachability/arvo_gdb.py` performs
execution and `reachability/core.py` performs deterministic scoring.
