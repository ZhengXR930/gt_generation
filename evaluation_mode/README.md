# evaluation_mode/ — observe & record, no construction control flow

Observes the OpenHands trajectory, records vulnerability reasoning with
`record_vulnerability_state`, binds submitted PoCs to the latest reasoning state,
and generates reachability artifacts for offline T1–T3 / reachability scoring.

**Candidate synthesis, the construction FSM, and the forced
plan/build/submit loop are all DISABLED in this mode.**

## Entry point
```bash
evaluation_mode/run_evaluation_harness_experiment.sh arvo:13730
```
This sources `shared/harness_mode_env.sh evaluation` (sets
`OPENHANDS_HARNESS_MODE=evaluation` and turns the enhance-only flags off), then
runs `shared/run_cybergym_openhands_deepseek.sh` per task.

## Contents
- `external_interpreter/` — offline interpreter: replays a trajectory, binds PoC
  attempts, and runs reachability (`-m reachability_eval.cli`).
  `run_external_interpreter.sh` is a thin CLI wrapper.
- `evaluator/` — T1–T5 reasoning/PoC scoring library (`-m evaluator.cli`). T5 is
  root-cause rationale only (patch evaluation has been removed).
- `reachability_eval/` — **R1-R4 reachability evaluation** (`evaluate_r1_r5` +
  the `-m reachability_eval.cli` runner). The gdb engine it calls lives in
  `shared/reachability_core`; only the R1-R4 scoring is here.
- `eval_mcp_servers/` — evaluator-side MCP servers: `reachability_recorder_server.py`
  (R1-R4), run via `run_reachability_recorder_mcp_server.sh`.
- `prompts/cybergym_reasoning_tool_prompt.txt` — the eval-mode agent prompt.

## Depends on (from shared/)
`recorder_core` and `reachability_core` (engine), the reasoning-recorder MCP
server, and the OpenHands controller/observer in `external/OpenHands`.

## PYTHONPATH (set by the entry script)
`shared : evaluation_mode : external/cybergym/src`
