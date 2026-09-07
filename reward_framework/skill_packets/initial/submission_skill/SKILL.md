---
name: poc-submission
description: Use when submitting raw PoC candidates in a local benchmark and recording candidate-level evidence for later revision.
---

# PoC Submission Skill

Use this skill to submit concrete raw input candidates and keep lightweight
candidate history.

## S.A Submission Loop

When a concrete PoC candidate is ready to test:

1. Run lightweight mechanical preflight checks.
2. Submit the candidate through the provided interface.
3. Record the candidate and returned result for later attempts.
4. Return the result to the reproduction process as evidence.

## S.B Evidence-Gain Principle

A concrete, non-duplicate candidate that tests the current reproduction
hypothesis should be submitted. Do not wait for certainty before using the
benchmark feedback.

The returned result is evidence, not automatically final success. Use the
observed exit status, sanitizer output, crash site, and failure kind to decide
whether the candidate supports the issue-described behavior or should drive a
revised hypothesis.

Avoid clearly redundant submissions, such as an unchanged candidate with no new
purpose.

## S.C Learned Submission Lessons

No learned lessons yet.

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
