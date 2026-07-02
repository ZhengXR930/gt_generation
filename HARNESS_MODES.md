# Harness Modes

This repository keeps evaluation and enhancement as separate execution modes.
The code is split into three top-level zones (see each dir's `README.md`):

```
evaluation_mode/   eval-only: external_interpreter, evaluator, eval prompt, entry script
enhance_mode/      enhance-only: candidate_synthesis_core, external_memory,
                   enhance_mcp_servers, FSM prepare/run scripts, enhance prompts
shared/            used by BOTH: recorder_core, reachability_core,
                   patch_evaluator_core, recorder/reachability MCP servers,
                   harness_mode_env.sh, run_cybergym_openhands_deepseek.sh
external/OpenHands the agent platform — its agent_controller.py holds BOTH modes'
                   logic, gated at runtime by OPENHANDS_HARNESS_MODE (not split)
scripts/           GT-generation / dataset tooling (mode-independent)
```

Mode is selected by `OPENHANDS_HARNESS_MODE` (`evaluation` | `enhance`), set by
`shared/harness_mode_env.sh` which each entry script sources.

## Evaluation Mode

Entry point:

```bash
evaluation_mode/run_evaluation_harness_experiment.sh arvo:13730
```

Purpose:

- observe the OpenHands trajectory;
- record vulnerability reasoning with `record_vulnerability_state`;
- bind submitted PoCs to the latest reasoning state;
- generate reachability artifacts for offline T1-T3 and reachability evaluation.

Disabled in this mode:

- candidate synthesis MCP;
- construction FSM;
- forced candidate plan/build/submit loop.

## Enhancement Mode

Entry point:

```bash
enhance_mode/run_enhancement_harness_experiment.sh arvo:12466
```

Purpose:

- use the observer to freeze a minimal vulnerability hypothesis;
- enforce candidate construction with the FSM:
  `reasoning -> record_candidate_plan -> build_candidate -> submit_candidate`;
- use H1-H5 reachability feedback to drive the next candidate plan;
- invoke construction-support requests after H1 parser/source reachability failures.

Enabled in this mode:

- candidate synthesis MCP;
- construction FSM;
- reasoning observer;
- H1-H5 feedback binding.
