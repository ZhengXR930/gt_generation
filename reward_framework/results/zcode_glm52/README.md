# ZCode + GLM-5.2 result registry

This registry preserves the canonical merged initial-skill TRAIN runs. The
retry, smoke, and partial continuation directories remain under
`reward_framework/harness_runs/_archive/non_dsh/` and are not mixed into these
results.

The two canonical runs are complete for generation and reachability evaluation:
batch000 has 7/10 sample-level GT triggers and batch001 has 8/10. They use the
same empty two-file initial packet as the DSH initial arm. No canonical ZCode
evolving or TEST arm has been produced yet, so those entries are intentionally
not fabricated.
