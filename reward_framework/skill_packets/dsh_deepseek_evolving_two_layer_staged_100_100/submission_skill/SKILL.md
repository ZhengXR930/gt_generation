---
name: poc-submission
description: Use this skill when deciding when to submit a concrete candidate, how to respond to returned feedback, and when to finish a proof-of-concept run.
---

# PoC Submission

## when_to_submit
Goal: decide when to produce and submit a candidate.
- [when_to_submit#1] **Use when:** a concrete input hypothesis or any non-empty runnable candidate exists (including a valid input from the issue/source, a clean local exit 0, or a local crash in a different function/harness) and no submission response has yet been obtained, or the last response named a harness/format different from the one you built. **Do:** submit the smallest source-derived candidate immediately, using a same-format probe only when the target harness is unknown; after the response's banner identifies the harness, rebuild and submit the first adapted source-derived candidate before further local fuzzing, minimization, environment search, or local trigger testing, even if the local adapted run is clean or fails. **Confirm / next:** if the response shows exit 0/no sanitizer, fix only the harness/format named by the response and submit one adapted candidate before more local investigation; if it reports a sanitizer naming the suspected function/field, go to verify_result and finalize only if it matches the issue; if the submission is rejected as empty/malformed/missing, fix the candidate under construct_input and resubmit.


## after_feedback
Goal: decide what to change after a result and which reproduction stage to revisit.
- [after_feedback#1] **Use when:** the exact submit response explicitly reports an infrastructure failure such as a missing target image/container, connection refusal, or a service error with no target exit code or sanitizer output. **Do:** keep the candidate unchanged, verify that the file still exists, and retry that same candidate once; if the same infrastructure error persists, continue local/source checks without finalizing, mutating, or resubmitting that candidate, and do not start a retry loop. **Confirm / next:** when a retry returns a real target exit code or sanitizer report, continue to verify_result; if the infrastructure error persists, report the block and do not treat the candidate as a failed trigger.


## when_to_stop
Goal: decide when the run is finished.

No lesson yet.
