# shared/ — code used by BOTH harness modes

Both `evaluation_mode` and `enhance_mode` depend on everything here. Do not put
mode-specific behavior in this directory.

## Packages
- `recorder_core/` — `record_vulnerability_state` reasoning recorder (events +
  reduced state). Used by the reasoning-recorder MCP server (here) and, in
  enhance mode, by `candidate_synthesis_core`.
- `reachability_core/` — the gdb reachability **engine** only: `run_gdb_reachability`,
  `gdb_reachability.py`, GT-checkpoint extraction, and sanitizer-trace parsing.
  Used by both eval (R1-R4 scoring) and enhance (H1-H5 feedback). The R1-R4
  **evaluation/scoring** (`evaluate_r1_r5`, the `-m reachability_eval.cli` runner,
  and the reachability MCP server) is eval-specific and lives in
  `evaluation_mode/reachability_eval/` + `evaluation_mode/eval_mcp_servers/`.

## MCP servers (`mcp_servers/`)
- `reasoning_recorder_server.py` — run via `run_reasoning_recorder_mcp_server.sh`
  (the R1-R4 reachability MCP server moved to `evaluation_mode/eval_mcp_servers/`).

## Scripts
- `harness_mode_env.sh` — sourced by each mode's entry script; sets
  `OPENHANDS_HARNESS_MODE` (+ the feature flags) for `evaluation` or `enhancement`.
- `run_cybergym_openhands_deepseek.sh` — the actual OpenHands/CyberGym launcher,
  invoked by both mode entry scripts.

## The OpenHands controller is also shared
`external/OpenHands` (the agent platform) is NOT under this dir but is equally
shared. Its `agent_controller.py` holds BOTH modes' logic, gated at runtime by
`OPENHANDS_HARNESS_MODE` — it is not physically split because both modes run on
the same controller. The `reasoning_observer.py` (recorder-scheduling observer)
also lives there.

## PYTHONPATH
Mode entry scripts put `shared/` (plus their own mode dir and
`external/cybergym/src`) on `PYTHONPATH`, so packages keep their top-level import
names (`import recorder_core`, etc.).
