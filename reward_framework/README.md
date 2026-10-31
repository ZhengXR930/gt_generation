# Reward Framework

The full ReproFocus method and evidence contract are documented in
[`REPROFOCUS_METHOD.md`](REPROFOCUS_METHOD.md). It describes the three-arm
controls, the single-sample Diagnostician, cross-sample Teacher, Curator gate,
Correction/propagation check, and the two-file initial/evolving packet design.

`reward_framework` is the skill-enabled evaluation frontend. It owns the reward
prompt, skill-packet installation, and run output namespace under
`harness_runs/<run_id>/`. The current default experiment target is
DeepSeek Harness (`deepseek_harness`) with `deepseek-v4-flash`, but harness and
model are configuration fields rather than a framework-wide restriction.
ZCode + GLM-5.2 initial results are maintained alongside the DSH results.

The framework has five adapter packages:

- `reward_framework.adapters.openhands`
- `reward_framework.adapters.codex`
- `reward_framework.adapters.claude`
- `reward_framework.adapters.deepseek_harness`
- `reward_framework.adapters.zcode`

All adapters call the neutral executors in `harness_runtime/` and install
the configured skill packet into the generated workspace before the agent
starts. The active packet contains two runtime skills, with four reproduction
stages and three submission stages installed together. The historical five-file
packet remains available only by explicit path. Baseline PoC generation lives
separately in `poc_generation/` and does not import these reward adapters.

## Canonical DSH task prompt

Every DSH + DeepSeek coding-agent run uses exactly
`reward_framework/prompt.txt`; its byte hash and consumers are recorded in
`reward_framework/PROMPT_META.json`. The files under
`distillation/prompt_templates/` are prompts for Diagnostician, Teacher,
Curator, and Correction role sessions only. They are not alternate task
prompts and must not be passed to the coding harness.

Initialize the current DSH reward split from the tracked baseline with:

```bash
python -m reward_framework.distillation.cli init-run \
  --run-dir reward_framework/distillation_runs/dsh_deepseek \
  --baseline-dir reward_framework/baselines/dsh_deepseek_failure_balanced100
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

For the canonical DSH + DeepSeek limits, use one of the two configs in
`reward_framework/configs/`: `dsh_deepseek_4096_max.json` or
`dsh_deepseek_32768_max.json`. Both default to the initial skill; select the
current evolving packet with `--skill-variant evolving`. If `--config` is
omitted, the runner uses `dsh_deepseek_4096_max.json`; pass the 32768 config
explicitly when the high-budget arm is required.
Distillation role sessions also default to 4096 output tokens; pass
`--role-max-output-tokens 32768` only for an intentional high-budget role run.
For a direct override, use `--max-output-tokens 4096` or
`--max-output-tokens 32768`; the default is 4096.

The tracked DSH baseline configuration is stored in
`reward_framework/baselines/dsh_deepseek_failure_balanced100/`. It is the
single active 100/150 split (100 TRAIN, 150 TEST) used by the current DSH reward
experiments. Use that directory as the source of truth for DSH batch
composition; the skill arm must always be identified by the packet hash in the
run manifest.
Ignored directories under `distillation_runs/` are local run artifacts. A
compact stable indexes of the current arms are kept in
`reward_framework/results/dsh_deepseek/RESULTS_INDEX.json` and
`reward_framework/results/zcode_glm52/RESULTS_INDEX.json`; raw harness output
remains in the run directories named by those indexes.


Each run config declares its model/provider fields directly. Endpoint values and
tokens are loaded at launch from `/home/xinran/token.txt`; configs name only
the required environment variables.

Use `--sample-selector valid_gt`, `valid_gt_arvo`, or `valid_gt_non_arvo` to
read `gt_results/valid_gt.json`, or pass explicit `--sample` /
`--samples-file` inputs.

Skill evolution lives in `distillation/`. The active packets are
`skill_packets/dsh_deepseek_initial_two_layer_staged_100_100/` and
`skill_packets/dsh_deepseek_evolving_two_layer_staged_100_100/`; each has a
`PACKET_META.json` with the packet content hash. Distillation runs materialize
their own `skill_packets/batch_NNN/` snapshots. The evolving stable path is
the final packet used by the 32768-token TEST arm. The five-file packets and
the older complete two-layer packets remain available only for historical
reproducibility.
Batch outputs under `distillation_runs/` and raw harness outputs under
`harness_runs/` are local artifacts and are intentionally ignored. Commit only
portable configs, code, prompts, and compact evaluation results.
