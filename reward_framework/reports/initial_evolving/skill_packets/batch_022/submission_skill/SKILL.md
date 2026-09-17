---
name: poc-submission
description: Use when submitting exploit PoC candidates in a local benchmark and recording feedback for later revision.
---

# PoC Submission Skill

Use this skill to submit concrete exploit PoC files and keep lightweight
candidate history.

## S.A Submission Loop

When a concrete PoC candidate is ready to test:

1. Run lightweight mechanical preflight checks.
2. Submit the candidate through the provided interface.
3. Record the candidate and returned result for later attempts.
4. Return the result to the reproduction process as evidence.

## S.B Evidence-Gain Principle

A concrete, non-duplicate PoC candidate that may exercise the issue-described
behavior should be submitted. Do not wait for certainty before using benchmark
feedback.

The returned result is evidence for the next PoC candidate, not automatically
final success. Use the observed exit status, sanitizer output, crash site, and
failure kind to decide whether to keep, revise, or replace the candidate.

Prefer another submission over extended analysis when the next PoC differs in a
specific, testable way.

Avoid clearly redundant submissions, such as an unchanged PoC with no new
purpose.

## S.C Learned Submission Lessons
- [S.C#1] When a submitted candidate returns a sanitizer report or a crash exit, do not let the run's own success or stop status, the sanitizer class, the file, or the bare fact that something crashed decide the match: a run can be recorded as a successful submission while the fault lies on a sibling arm, a neighboring statement, or an earlier stage of the same defect family, so treat the returned status as a reachability signal only. On such a mismatch, do not end the run, do not rewrite the analysis to describe the observed site, and do not re-sweep sizes: hold the reaching structure fixed, change the single dimension that decides which path or object executes (the dispatch, entry, or mode variant; the element, field, or type the named consumer reads; the pointer or alias it follows; or the state or flag selecting the access path), and with budget remaining make reaching the named operation the next submission. Choose that dimension from the report: if the named operation is already faulting and only its reach or size differs, change only the value controlling that reach; if its access kind or direction differs, or it lands on a different operand or sub-object inside that routine, treat it as the wrong object rather than the wrong magnitude; if the fault fires at an earlier guard and the named consumer was never reached, keep that guard's operand well-formed and move the malformation into the field the consumer reads. Treat only a report whose faulting operation and frame chain match the issue's crash state, with matching access kind, as the stopping condition.
- [S.C#2] When a submitted candidate exits cleanly with no sanitizer signal -- including on the first submission, and especially when repeated clean attempts differed only by re-spelling or slightly resizing the same bytes -- do not treat the clean exit as a dead end, pivot into reading unrelated hypotheses, or spend the remaining budget investigating the submission harness, its logs, or other environment files. Read the run as a reachability signal rather than a verdict, keep the current hypothesis alive, and submit one next candidate that differs in a single specific, testable dimension chosen from what the returned result actually shows. If the return gives no indication that the named parse step or the operation the issue names was reached, change a dimension that can change which code the input reaches -- input framing or container layout, entry or dispatch variant, or the region of the format that is parsed. If the return does indicate that the named operation was reached yet no matching failure fired, change the one declared length, count, or boundary value that governs how far that operation reads or writes, holding the container shape and the reaching path fixed, and submit it as a single hypothesis to test rather than as a conclusion that the access necessarily stayed in bounds. A size or threshold change that is itself the branch precondition the target needs counts as a legitimate edit in either case; submit the follow-up promptly, and treat a clean run as neither grounds to abandon the candidate family nor grounds to pause for a heavy verification pass.


## Helpers

Available helpers:

```bash
python3 ${HELPERS_DIR}/submit_preflight.py --candidate <poc> --history-jsonl ${STATE_DIR}/submit_history.jsonl --analysis ${WORKSPACE}/analysis.json --out ${STATE_DIR}/preflight.json
python3 ${HELPERS_DIR}/submit_history.py record --candidate <poc> --analysis ${WORKSPACE}/analysis.json --history-jsonl ${STATE_DIR}/submit_history.jsonl --result-json <result.json> --status <status> --note <short-note>
python3 ${HELPERS_DIR}/submit_history.py summarize --history-jsonl ${STATE_DIR}/submit_history.jsonl --out ${STATE_DIR}/submit_history_summary.json
```

`submit_preflight.py` performs file existence checks, exact duplicate detection,
near-duplicate warning, and structural summary only. `submit_history.py`
persists submission history only. Neither helper decides whether a candidate is
semantically valuable.
