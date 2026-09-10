# Reward Framework

`reward_framework` is the skill-enabled evaluation frontend. It owns the reward
prompt, skill-packet installation, and run output namespace under
`harness_runs/<run_id>/`. The current default experiment target is
DeepSeek Harness (`deepseek_harness`) with `deepseek-v4-flash`.

The framework has four adapter packages:

- `reward_framework.adapters.openhands`
- `reward_framework.adapters.codex`
- `reward_framework.adapters.claude`
- `reward_framework.adapters.deepseek_harness`

All four adapters call the neutral executors in `harness_runtime/` and install
the Reproduction Skill and Submission Skill packet into the generated workspace
before the agent starts. Baseline PoC generation lives separately in
`poc_generation/` and does not import these reward adapters.

Initialize the current DSH reward split from the tracked baseline with:

```bash
python -m reward_framework.distillation.cli init-run \
  --run-dir reward_framework/distillation_runs/dsh_deepseek \
  --baseline-dir reward_framework/baselines/deepseek_harness_v4_flash
```

Run a DSH reward-framework smoke batch with:

```bash
python -m reward_framework.run_harness \
  --harness deepseek_harness \
  --model deepseek-v4-flash \
  --run-id reward-dsh-smoke \
  --sample-selector valid_gt_arvo \
  --limit 1 \
  --parallel 1 \
  --dry-run
```

The tracked DSH baseline configuration is stored in
`reward_framework/baselines/deepseek_harness_v4_flash/`. It contains the
balanced TRAIN split and baseline per-batch summary used for the current DSH
reward experiments. Use that directory as the source of truth for DSH batch
composition; ignored directories under `distillation_runs/` are local run
artifacts.


Model/provider settings are shared with baseline PoC generation through `model_router/`. Prefer `--model-route`; direct model/provider flags are kept for explicit overrides.

Use `--sample-selector valid_gt`, `valid_gt_arvo`, or `valid_gt_non_arvo` to
read `gt_results/valid_gt.json`, or pass explicit `--sample` /
`--samples-file` inputs.

Skill evolution lives in `distillation/`. The checked-in initial packet lives in
`skill_packets/initial/`; batch outputs under `distillation_runs/` and raw
harness outputs under `harness_runs/` are local artifacts and are intentionally
ignored. Commit only portable configs, code, prompts, and compact evaluation
results.
