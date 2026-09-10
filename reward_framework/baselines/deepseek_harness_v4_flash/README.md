# DeepSeek Harness + DeepSeek v4 Flash Reward Baseline

This directory is the tracked, portable baseline for the current reward-framework experiments.

- `run_config.json`: canonical DSH coding-agent and distiller model configuration.
- `split_manifest.json`: balanced 30-batch TRAIN split derived from `poc_generation/poc_results/deepseek-harness-v4-flash`.
- `batch_summary.json`: per-batch baseline outcome summary for the DSH baseline.

Large run artifacts remain under ignored `reward_framework/harness_runs/` and `reward_framework/distillation_runs/`.
