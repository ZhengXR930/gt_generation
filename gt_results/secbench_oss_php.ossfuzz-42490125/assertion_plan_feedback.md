# Assertion Plan Feedback for secbench_oss_php.ossfuzz-42490125

Stage 04B could not verify the frozen assertion plan. The next Stage 04A run must rewrite the semantic assertion plan; do not reuse the failed predicate, event placement, or instrumentation expression unchanged.

## Failure Class

root_not_violated_on_vulnerable_witness

## Stage 04B Diagnosis

- classification: semantic_root_failure
- message: At least one required root predicate was already satisfied or not exercised on the vulnerable original witness. Stage 04A must choose a safety obligation that the vulnerable crashing run violates before the protected operation.
- evidence: [{"id": "A_ROOT_SCOPE_NULL", "left": 91328184666136, "op": "eq", "reason": "required root predicate is not violated on the vulnerable original witness", "right": 0, "vulnerable_status": "guarded"}]

## Failed Assertions

### A_ROOT_SCOPE_NULL
- kind: required
- verification_error: fixed original is guarded; add exactly one perturbation case before accepting the guarded fixed-side witness
- vulnerable original: {"left": 91328184666136, "op": "eq", "right": 0, "satisfied": true, "status": "guarded", "triggered": false}
- fixed original: {"left": 91328184666136, "op": "eq", "right": null, "satisfied": true, "status": "guarded", "triggered": false}

### A_SINK_TABLE_MATCH
- kind: transition
- vulnerable original: {"from": "lookup", "ordered": false, "satisfied": false, "status": "out_of_order", "to": "sink"}
- fixed original: {"satisfied": null, "status": "not_exercised"}

## Required Stage 04A Repair

1. Re-read `ground_truth.json`, `sanitizer_trace.txt`, `reproduction_report.json`, and the vulnerable source.
2. Redesign the `required` root obligation so vulnerable original violates it when the protected operation runs.
3. Ensure the fixed original satisfies the same obligation or avoids the protected operation through a real guard.
4. Move any protected event to immediately before the dangerous operation and after every guard that can skip it.
5. Avoid instrumentation expressions that are known to fail compilation; bind source variables or simple runtime fields instead.
6. Regenerate `candidate_assertions.json`, `candidate_invariants.json`, `field_bindings.json`, `event_locations.json`, `.assertion_spec_frozen.json`, and `assertion_preflight.json` from scratch.
