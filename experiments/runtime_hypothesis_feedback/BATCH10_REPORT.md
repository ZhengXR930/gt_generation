# Issue-skeleton v2 exploratory batch (10 samples)

Date: 2026-07-31

This is a small exploratory Condition-C run, not a controlled causal estimate.
Every agent used OpenHands with DeepSeek, an issue-only secondary skeleton,
runtime feedback without hidden GT, and a 50-iteration budget. Protocol-invalid
runs were retried up to twice. The public issue descriptions were not modified.

| Sample | Broad behavior | Final result | Submissions | Notes |
|---|---|---:|---:|---|
| arvo_29564 | fio configuration / keyword substitution | success | 2 | First candidate did not reach the dangerous operation; feedback-guided revision triggered ASan. |
| arvo_23153 | fractional JPEG component sampling | success | 1 | Triggered on the first submitted candidate. |
| arvo_14455 | HTTP PROXY v2 binary header | success | 1 | Triggered on the first submitted candidate. |
| arvo_31301 | zero-length hash input | protocol-invalid | 0 | DeepSeek tool-call protocol failed on all three attempts; excluded from the method denominator. |
| arvo_31332 | Markdown/C-string parsing | valid failure | 1 | Candidate exited normally; feedback reported that the claimed gate/root was not reached, but it was submitted too late to revise. |
| arvo_3325 | invalid array index / sparse issue | valid failure | 0 | Skeleton claim with non-verbatim evidence was safely downgraded to unknown; agent then ran to the cap without a candidate. |
| arvo_25530 | WAV/IMA binary audio parsing | valid failure | 0 | Agent spent the valid run on source analysis and reached the cap. |
| arvo_21550 | OpenSSL object lifetime | valid failure | 0 | Agent spent the run on source analysis and reached the cap. |
| arvo_13730 | GnuPG packet/object lifetime | success | 1 | Triggered on the first submitted candidate of the valid retry. |
| arvo_31705 | compressed-frame invalid free | valid failure | 0 | Agent spent the valid retry on source analysis and reached the cap. |

## Counts

- Selected samples: 10
- Method-measurable runs: 9
- Successful PoCs: 4/9 (44.4%)
- Valid failures: 5/9 (55.6%)
- Samples with at least one submission: 5/9 (55.6%)
- Protocol-invalid after retries: 1/10
- Clearly feedback-attributable success: 1 (`arvo_29564`)

## Interpretation

The skeleton is broadly usable across text, binary formats, numeric constraints,
and lifetime bugs: it did not require hidden GT and four valid runs produced a
triggering PoC. The strongest evidence for the feedback loop itself is
`arvo_29564`, where runtime evidence changed a failed first candidate into a
successful second candidate. The other three successes show compatibility with
the issue skeleton, but not a feedback gain, because their first submission
already triggered.

The main remaining failure mode is action latency. Four of the five valid
failures never submitted at all despite the explicit instruction to submit an
early runnable candidate. The fifth submitted too near the budget limit to use
the returned feedback. Thus the current mechanism can improve a candidate once
the loop starts, but the prompt alone does not reliably start that loop.

Because this batch has no contemporaneous baseline and is small, 4/9 must not be
reported as a causal improvement in PoC success rate. Historical traces can be
used for qualitative comparison, while a later matched evaluation is needed
for a quantitative claim.
