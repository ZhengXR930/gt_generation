# Harness adapters

Adapters are the stable integration layer between a harness, baseline PoC generation, and the learned
skill packet.

- Skill packets contain learned/reusable behavior.
- Adapters install and expose those packets inside a concrete harness workspace.
- Adapters build PoC-generation commands for each harness so batch launchers do not patch per-machine scripts.
- Adapters must not contain sample-specific lessons, GT traces, hidden oracle
  constants, or test-set evidence.

Each adapter should provide:

- a contract module defining workspace paths and interface semantics;
- an installer that copies skills/helpers and wraps submit safely;
- a validator that must pass before model test runs;
- optional telemetry readers for Teacher observation packaging.


Current support level:

| Adapter | Current status | What is validated |
| --- | --- | --- |
| OpenHands | Runnable reward-framework adapter. Uses an isolated runner under `reward_framework.adapters.openhands.run_samples`. | Skill packet validation, workspace install/wrapper interface, dry-run command path, result-root separation. |
| Codex | CLI-agent PoC runner scaffold via `poc_generation/poc_generator/run_cli_sample.py`, plus native skill export. | Command construction, ModelHub bridge path, native skill export, helper copy/validation. |
| Claude | CLI-agent PoC runner scaffold via `poc_generation/poc_generator/run_cli_sample.py`, plus native skill export. | Command construction, Claude config/skill export, optional project bridge, helper copy/validation. |
| DeepSeek Harness | CLI-agent PoC runner scaffold via `poc_generation/poc_generator/run_cli_sample.py`, plus DSH bundle/plugin export. | Command construction, bundle layout, skill export, manifest, Cordis patch/plugin file shape. |

Do not treat export-only adapters as end-to-end benchmark runners. To test a
non-OpenHands harness, add a harness-specific runner under that adapter which
writes only to `reward_framework/harness_runs/<run_id>/`.

Result separation rule:

- Baseline `poc_generation` evaluations write to `poc_generation/poc_results`.
- Reward-framework adapter evaluations must write to
  `reward_framework/harness_runs/<run_id>/`.
- Adapter launchers may reuse low-level harness executors such as
  `run_sample.py`, but they must pass an explicit adapter-owned `--results-dir`
  and keep status/log/manifest files under the reward-framework run directory.
