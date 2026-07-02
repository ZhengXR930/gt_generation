#!/usr/bin/env bash
# Source this file from runner scripts to select one harness mode.
#
# Modes:
#   evaluation  - observe/record reasoning and PoC submissions without changing
#                 the candidate construction control flow.
#   enhancement - enable the FSM/controller that drives
#                 hypothesis -> candidate plan -> build -> submit -> feedback.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "source scripts/harness_mode_env.sh <evaluation|enhancement>" >&2
  exit 2
fi

_harness_mode="${1:-}"
if [[ -z "${_harness_mode}" ]]; then
  echo "harness_mode_env.sh requires mode: evaluation or enhancement" >&2
  return 2
fi

if [[ -z "${ROOT_DIR:-}" ]]; then
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

case "${_harness_mode}" in
  evaluation)
    export OPENHANDS_HARNESS_MODE=evaluation
    export OPENHANDS_HARNESS_FSM=0
    export CYBERGYM_ENABLE_CANDIDATE_SYNTHESIS_MCP=0
    export OPENHANDS_ENFORCE_CONSTRUCTION_LOOP=0
    export OPENHANDS_ENHANCEMENT_STAGE_CONTROLLER=0
    export OPENHANDS_HARNESS_LARGE_READ_GUARD=0
    export OPENHANDS_ENABLE_REASONING_RECORDER=1
    export OPENHANDS_REASONING_RECORDER_POLICY="${OPENHANDS_REASONING_RECORDER_POLICY:-observer}"
    export OPENHANDS_REASONING_RECORDER_INTERVAL="${OPENHANDS_REASONING_RECORDER_INTERVAL:-4}"
    export CYBERGYM_OPENHANDS_PROMPT_FILE="${CYBERGYM_OPENHANDS_PROMPT_FILE:-${ROOT_DIR}/evaluation_mode/prompts/cybergym_reasoning_tool_prompt.txt}"
    unset CANDIDATE_SYNTHESIS_REACHABILITY_DEBUG_COMMAND
    unset CANDIDATE_SYNTHESIS_REACHABILITY_COVERAGE_COMMAND
    unset CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND
    unset CANDIDATE_SYNTHESIS_SUBMIT_SERVER
    ;;
  enhancement)
    # The OpenHands controller currently uses the short value "enhance".
    export OPENHANDS_HARNESS_MODE=enhance
    export OPENHANDS_HARNESS_FSM=1
    export CYBERGYM_ENABLE_CANDIDATE_SYNTHESIS_MCP=1
    export OPENHANDS_ENFORCE_CONSTRUCTION_LOOP=1
    export OPENHANDS_ENHANCEMENT_STAGE_CONTROLLER="${OPENHANDS_ENHANCEMENT_STAGE_CONTROLLER:-1}"
    export OPENHANDS_HARNESS_LARGE_READ_GUARD="${OPENHANDS_HARNESS_LARGE_READ_GUARD:-1}"
    export OPENHANDS_ENABLE_REASONING_RECORDER=1
    export OPENHANDS_REASONING_RECORDER_POLICY="${OPENHANDS_REASONING_RECORDER_POLICY:-observer}"
    export OPENHANDS_REASONING_RECORDER_INTERVAL="${OPENHANDS_REASONING_RECORDER_INTERVAL:-4}"
    export CYBERGYM_ENABLE_CANDIDATE_SYNTHESIS_MCP=1
    export CANDIDATE_SYNTHESIS_ALLOW_UNGUIDED="${CANDIDATE_SYNTHESIS_ALLOW_UNGUIDED:-0}"
    export CANDIDATE_SYNTHESIS_REQUIRE_DEBUG_REACHABILITY="${CANDIDATE_SYNTHESIS_REQUIRE_DEBUG_REACHABILITY:-0}"
    export CYBERGYM_OPENHANDS_PROMPT_FILE="${CYBERGYM_OPENHANDS_PROMPT_FILE:-${ROOT_DIR}/enhance_mode/prompts/cybergym_fsm_construction_prompt.txt}"
    ;;
  *)
    echo "unknown harness mode: ${_harness_mode}" >&2
    return 2
    ;;
esac

export OPENHANDS_REASONING_OBSERVER_MODEL="${OPENHANDS_REASONING_OBSERVER_MODEL:-gpt-5.4-2026-03-05}"
export OPENHANDS_REASONING_OBSERVER_CONFIG="${OPENHANDS_REASONING_OBSERVER_CONFIG:-${ROOT_DIR}/config.txt}"
export CYBERGYM_PREEXTRACT_REPO_TAR="${CYBERGYM_PREEXTRACT_REPO_TAR:-1}"
export OPENHANDS_RUNTIME_CONTAINER_IMAGE="${OPENHANDS_RUNTIME_CONTAINER_IMAGE:-cybergym-openhands-runtime:0.33-skip-root-chown}"
export OPENHANDS_NATIVE_TOOL_CALLING="${OPENHANDS_NATIVE_TOOL_CALLING:-true}"
