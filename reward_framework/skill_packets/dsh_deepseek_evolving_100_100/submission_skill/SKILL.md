---
name: poc-submission
description: Use when submitting raw PoC candidates and recording their feedback.
---

# PoC Submission Skill

The canonical decision policy is the reproduction skill's **Decision state
machine**. This skill only defines submission mechanics so that stopping and
revision rules are not duplicated or contradictory.

## Submission mechanics

When a candidate is ready:

1. Run lightweight mechanical preflight checks.
2. Submit the raw candidate through the provided interface.
3. Record the candidate and returned response.
4. Return the response to the reproduction state machine as evidence.

Every later submission must be non-duplicate and justified by one new source
finding or returned-feedback fact. Do not submit batches of syntax, field,
boundary, size, or offset mutations that test the same unresolved condition.
Before calling `submit.sh`, verify that the reproduction state records that
evidence delta. Do not submit while the active state is `NO_PROGRESS`,
`ANALYSIS_STALL`, or `TRACE_CONFLICT` unless the state machine has produced
the single source-backed revision allowed for that state.

## Helpers

Available helpers:

```bash
python3 ${HELPERS_DIR}/submit_preflight.py --candidate <poc> --history-jsonl ${STATE_DIR}/submit_history.jsonl --analysis ${WORKSPACE}/analysis.json --out ${STATE_DIR}/preflight.json
python3 ${HELPERS_DIR}/submit_history.py record --candidate <poc> --analysis ${WORKSPACE}/analysis.json --history-jsonl ${STATE_DIR}/submit_history.jsonl --result-json <result.json> --status <status> --note <short-note>
python3 ${HELPERS_DIR}/submit_history.py summarize --history-jsonl ${STATE_DIR}/submit_history.jsonl --out ${STATE_DIR}/submit_history_summary.json
```

`submit_preflight.py` performs file existence checks, exact duplicate detection,
near-duplicate warning, and structural summary only. `submit_history.py` stores
submission history only; neither helper decides whether a candidate matches the
vulnerability.
