# Initial vs Evolving Training Results

This directory is the git-trackable projection of the local `dsh_deepseek_initial_batch000` distillation run. Raw harness logs, subsessions, source trees, and side experiments remain ignored.

`summary.json` is recomputed from the current canonical batch evaluations and the full initial-skill training run. `batches/batch_XXX.json` contains per-sample deterministic outcome summaries for initial skill versus the evolving skill used on that batch.

Included skill packets are the canonical chain only: `batch_000` through `batch_030`, plus `finalized_through_batch_029`. Historical side packets such as correction probes and strict-curator experiments are intentionally excluded.
