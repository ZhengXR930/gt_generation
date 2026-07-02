# enhance_mode/ — observer-frozen hypothesis + candidate construction FSM

Uses the observer to freeze a minimal vulnerability hypothesis, then enforces
candidate construction through the FSM
`reasoning -> record_candidate_plan -> build_candidate -> submit_candidate`,
driven by H1–H5 reachability feedback.

**Enabled in this mode:** candidate synthesis MCP, construction FSM, reasoning
observer, H1–H5 feedback binding.

## Entry point
```bash
enhance_mode/run_enhancement_harness_experiment.sh arvo:12466
```
This sources `shared/harness_mode_env.sh enhancement` (sets
`OPENHANDS_HARNESS_MODE=enhance` + FSM/candidate flags), then runs
`shared/run_cybergym_openhands_deepseek.sh` per task with retry handling.

## Contents
- `candidate_synthesis_core/` — candidate plan/build/synthesis logic. Resolves
  `gdb_reachability.py` via the `reachability_core` package (in `shared/`), and
  reads format memory from `enhance_mode/external_memory/`.
- `external_memory/format_construction/` — format-construction memory
  (e.g. `rar5.json`) consumed by `candidate_synthesis_core`.
- `enhance_mcp_servers/` — enhance-only MCP servers (package
  `enhance_mcp_servers`): `candidate_synthesis_server.py`. Launched by
  `run_candidate_synthesis_mcp_server.sh`.
- `prepare_enhancement_runner.py` — builds `enhancement_runner.json` (debug /
  coverage / sanitizer commands) consumed by the entry script.
- `prompts/` — `cybergym_fsm_construction_prompt.txt` (agent prompt).

## Depends on (from shared/)
`recorder_core` and `reachability_core` (engine), and the OpenHands
controller/observer in `external/OpenHands`.

## PYTHONPATH (set by the entry / MCP scripts)
`shared : enhance_mode : external/cybergym/src`
