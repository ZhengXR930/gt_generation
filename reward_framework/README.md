# Reward Framework

`reward_framework` is the skill-enabled evaluation frontend. It owns the reward
prompt, skill-packet installation, and run output namespace under
`harness_runs/<run_id>/`.

The framework has four adapter packages:

- `reward_framework.adapters.openhands`
- `reward_framework.adapters.codex`
- `reward_framework.adapters.claude`
- `reward_framework.adapters.deepseek_harness`

All four adapters call the neutral executors in `harness_runtime/` and install
the Reproduction Skill and Submission Skill packet into the generated workspace
before the agent starts. Baseline PoC generation lives separately in
`poc_generation/` and does not import these reward adapters.

Run a reward-framework batch with:

```bash
python -m reward_framework.run_harness \
  --harness openhands \
  --model gpt-5.4-mini-2026-03-17 \
  --run-id reward-smoke \
  --sample-selector valid_gt_arvo \
  --parallel 1
```

Use `--sample-selector valid_gt`, `valid_gt_arvo`, or `valid_gt_non_arvo` to
read `gt_results/valid_gt.json`, or pass explicit `--sample` /
`--samples-file` inputs.

`offline_static_distillation/` remains responsible for producing and validating
the frozen skill packet used by the adapters.
