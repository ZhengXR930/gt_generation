# Harness Adapters

Adapters are the stable integration layer between a concrete harness and the
reward-framework skill packet.

- Skill packets contain learned/reusable behavior.
- Adapters install and expose those packets inside a concrete harness workspace.
- Adapters build skill-enabled evaluation commands for each harness so batch
  launchers do not patch per-machine scripts.
- Adapters must not contain sample-specific lessons, GT traces, hidden oracle
  constants, or test-set evidence.

Each adapter should provide:

- a contract module defining workspace paths and interface semantics;
- an installer that copies skills/helpers and wraps submit safely;
- a validator that must pass before model test runs.


Current support level:

| Adapter | Current status | What is validated |
| --- | --- | --- |
| OpenHands | Runnable adapter via `harness_runtime/openhands/{arvo,local}.py`. | Skill packet validation, workspace install/wrapper interface, dry-run command path, result-root separation. |
| Codex | Runnable ARVO adapter via `harness_runtime/cli.py`, plus native skill export. | Command construction, ModelHub bridge path, native skill export, helper copy/validation. |
| Claude | Runnable ARVO adapter via `harness_runtime/cli.py`, plus native skill export. | Command construction, Claude config/skill export, optional project bridge, helper copy/validation. |
| DeepSeek Harness | Runnable adapter via `harness_runtime/deepseek_harness/{arvo,local}.py`, plus DSH bundle/plugin export. | Command construction, bundle layout, skill export, manifest, Cordis patch/plugin file shape. |

Run reward-framework harness evaluations through:

```bash
python -m reward_framework.run_harness \
  --harness openhands \
  --model gpt-5.4-mini-2026-03-17 \
  --run-id reward-smoke \
  --sample-selector valid_gt_arvo
```

Result separation rule:

- Baseline `poc_generation` evaluations write to `poc_generation/poc_results`.
- Reward-framework adapter evaluations must write to
  `reward_framework/harness_runs/<run_id>/`.
- Adapter launchers may reuse neutral executors from `harness_runtime`, but they
  must pass an explicit adapter-owned `--results-dir` and keep status/log/manifest
  files under the reward-framework run directory.
