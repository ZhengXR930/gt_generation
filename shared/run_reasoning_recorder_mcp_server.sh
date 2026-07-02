#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENHANDS_REPO="${ROOT_DIR}/external/OpenHands"

HOST="${RECORDER_MCP_HOST:-127.0.0.1}"
PORT="${RECORDER_MCP_PORT:-9001}"
EVENTS_PATH="${RECORDER_EVENTS_PATH:-${ROOT_DIR}/mcp_reasoning_events.jsonl}"
STATE_PATH="${RECORDER_STATE_PATH:-${ROOT_DIR}/mcp_reasoning_state.json}"

export PYTHONPATH="${ROOT_DIR}/shared${PYTHONPATH:+:${PYTHONPATH}}"

cd "${OPENHANDS_REPO}"
exec poetry run python "${ROOT_DIR}/shared/mcp_servers/reasoning_recorder_server.py" \
  --host "${HOST}" \
  --port "${PORT}" \
  --events-path "${EVENTS_PATH}" \
  --state-path "${STATE_PATH}"
