# Not The DSH Baseline

This directory is a historical DeepSeek result set from the legacy runner.
It should not be used as the DSH+DeepSeek no-skill baseline.

Observed 2026-10-04:

- 500 valid-GT sample directories.
- 500 `manifest.json`, `analysis.json`, and `context_visit.json`.
- 132 `reachability_eval.json`.
- 72 reachability GT-triggered samples.
- Manifests generally have no `harness` field.
- Protocols are mostly `poc_trace_per_submission_v2`.

The 132 reachability files correspond exactly to the 132 samples with
non-empty `deduplicated_pocs`. The remaining samples were not materialized as
zero-candidate reachability reports by the old post-processing path.

Use `../deepseek-harness-v4-flash/` for DSH+DeepSeek no-skill comparisons.
