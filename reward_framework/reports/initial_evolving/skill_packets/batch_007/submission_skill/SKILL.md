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
- [S.C#1] When a submitted candidate crashes, compare the runtime report with the crash state the issue names before treating it as final: a matching sanitizer class and a matching containing function are not enough. Check the dispatch/entry path the crash arrived through, the faulting statement, and the origin of the value the report names. If the crash arrived through a different entry or component than the issue names, the crash is not evidence about the issue-described path: do not stop on the crash exit code, record it as evidence, and make the next candidate enter through the issue-named path instead of spending more variants inside the family that crashed there. If the entry path lines up but the faulting statement or the value origin does not, treat it as a non-target crash and spend one bounded variant that routes through the issue-named statement or origin. Line numbers and inlining can shift, so a near-match inside the same component is a reason to spend that one variant, not proof the candidate is wrong. If the crash state lines up, keep that candidate as your best current evidence, but do not declare final success from your own reading of the crash; let the submission result confirm it, and do not chase the remaining gap with repeated near-identical variants.


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
