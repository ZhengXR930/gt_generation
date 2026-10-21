---
name: poc-reproduction
description: Use when constructing and revising a raw PoC for a local benchmark issue.
---

# PoC Reproduction Skill

Use the issue description, workspace source, and actual submission feedback to
construct one raw PoC input and improve it without confusing a crash with a
target match.

## Evidence-first loop

1. Read the issue description. Identify the input grammar/container, the issue
   clue, and the vulnerable source function and condition connecting them.
2. Form one active hypothesis naming all three. A generic boundary value or a
   syntactically different string is not a new hypothesis by itself.
3. Build and submit one focused, format-complete raw input once the hypothesis
   is concrete. Local diagnostics should establish the next edit, not become a
   separate open-ended build or fuzzing project.
4. Preserve the parts that reached relevant code and change only the
   issue-relevant field, condition, ordering, or dependent structure justified
   by new source or runtime evidence.
5. Keep source edits out of the candidate path; the benchmark evaluates the
   submitted input against its own target.

## Decision state machine

Maintain exactly one of these states and record the transition after every
submission. The record must name the active hypothesis, the new evidence
since the previous submission, the deepest verified checkpoint, and the
revision number for that hypothesis. A submission without a new evidence
delta is not a valid transition.

### HYPOTHESIS_READY -> SUBMIT

Submit one minimal, non-duplicate, format-complete candidate when the input
container and an issue/source-backed condition are ready. Do not wait for
certainty, but do not submit a format-only probe unless it answers a specific
harness-framing question.

### SUBMIT -> UNTESTED

If a runner/container error has no target execution output, the candidate was
untested, not disproved. Retry at most once only when the error looks transient.
If it persists, preserve the candidate, avoid identical resubmits and
open-ended environment work, and return to HYPOTHESIS_READY with the next
source-backed candidate.

### SUBMIT -> CLEAN_NEAR_MISS

If execution is clean or only proves input admission, treat it as evidence for
the path actually exercised. Identify one new source fact or returned-feedback
fact and make one condition-bearing revision. If no concrete new condition
remains, switch the structural hypothesis; never enumerate scalar/offset/size
variants of the same unresolved dependency.

### SUBMIT -> FALSE_POSITIVE

If the process exits nonzero but the observed operation, violated value/state/
lifetime, or source mapping is not consistent with the issue-described defect,
the result is a false positive/near miss. **Do not finalize and do not treat the
exit code or sanitizer class alone as success.**

If a source-backed revision is available, the next action is mandatory:

1. preserve the response as evidence;
2. change one prerequisite, paired field, state, ordering, or encoding that
   explains the mismatch;
3. submit one focused, non-duplicate revision.

If that one revision still does not establish the issue mechanism, switch
hypotheses rather than repeating the rejected bytes. If no source-backed
revision exists, switch or stop as unresolved; never claim success from the
crash form alone.

### SUBMIT -> TRACE_CONFLICT

If the evaluator reports a target flag or sink hit while a required earlier
checkpoint is absent (for example, a target flag with only R1/R2 evidence),
the result is an oracle/trace conflict, not a target match. Preserve the raw
report, do not finalize, and do not infer the missing source or root-cause
step from a later raw hit. Make at most one focused revision only if a
source-backed change can address the missing checkpoint; otherwise switch or
stop as unresolved.

### SUBMIT -> TARGET_MATCH -> FINALIZE

Finalize only when the visible failure is consistent with the issue-described
mechanism and the source mapping supports that connection. A different stack
or caller shape alone is not a mismatch when the issue leaves those details
unspecified, but a different operation or violated condition is.

### Any state -> NO_PROGRESS

After two consecutive candidates for the same mechanism fail to deepen the
verified reachability, change the failure stage, or add a new source/runtime
fact, mark the hypothesis as no-progress. Do not submit format-only controls,
unrelated crash probes, or scalar/size/offset mutations to keep the loop
alive. Return to HYPOTHESIS_READY only with a genuinely different,
source-backed mechanism; otherwise go to UNRESOLVED.

### Any state -> ANALYSIS_STALL

After the first candidate, every source read, build, or local diagnostic must
answer a named missing condition for the active hypothesis. Two consecutive
analysis cycles with no new condition are an analysis stall: stop broad grep,
environment probing, and harness-control experiments, then switch or stop as
unresolved. A crash from an unrelated control probe is feedback about the
harness, not evidence for the issue.

### Any state -> UNRESOLVED

If the interaction budget is exhausted, stop as unresolved rather than turning
the last crash into a success claim. Do not continue after a confirmed target
match merely to search for a prettier trace.

If the issue description gives only a sanitizer class, fuzzer name, or broad
operation, remain in HYPOTHESIS_READY until the source function and violated
value/state/lifetime condition are identified. A same-class crash alone never
justifies a submission or a target match.

## Last-mile encoding lesson

For a structured candidate, inspect the parser-visible bytes before the next
submission: container, field value, byte order, enclosing length, and dependent
sizes must encode the intended trigger. After one encoding-corrected revision
still runs cleanly with no new signal, change the failure mechanism or
dependency instead of varying size, index, offset, or enclosing length around
the same structure.

## Raw-candidate and trace rule

The candidate is only the raw input file. Never fabricate, attach, request, or
wait for an assumed `candidate_trace`, stack trace, or trace metadata. Use the
actual submission response and source mapping. A trace/reachability report is
evidence for the decision state, not part of the submitted artifact.
