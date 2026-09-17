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
- [S.C#1] When a submitted candidate returns a sanitizer report or crash exit, treat the returned status, the sanitizer class, the file, and the fact that something faulted as reachability evidence rather than automatic success. If the report corresponds to the issue's faulting access and failure class, keep it; if it clearly fires on a neighboring operation, do not end the run or rewrite the analysis around the observed site -- hold the reaching structure fixed, change the one dimension that decides which path or object executes, and make that revision the next submission.
- [S.C#2] When a candidate returns clean, read it as reachability evidence rather than a verdict: keep the current hypothesis alive and make one targeted follow-up submission instead of pivoting to unrelated hypotheses, harness or environment inspection, or a heavy verification pass. If several clean returns varied only in size, offset, length, or count and all stalled at the same admitted-but-silent point, change what the input contains -- add or repair the record, entry, section, or field the named path consumes -- rather than re-scaling. If the named operation was reached and only the failure is missing, change the one declared length, count, or boundary value governing how far it reads or writes.

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
