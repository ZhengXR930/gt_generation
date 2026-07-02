#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENHANDS_REPO="${ROOT_DIR}/external/OpenHands"

HOST="${REACHABILITY_MCP_HOST:-127.0.0.1}"
PORT="${REACHABILITY_MCP_PORT:-9012}"
OUT_DIR="${REACHABILITY_OUT_DIR:-${ROOT_DIR}/reachability_runs}"

export PYTHONPATH="${ROOT_DIR}/evaluation_mode:${ROOT_DIR}/shared${PYTHONPATH:+:${PYTHONPATH}}"

cd "${OPENHANDS_REPO}"
exec poetry run python "${ROOT_DIR}/evaluation_mode/eval_mcp_servers/reachability_recorder_server.py" \
  --host "${HOST}" \
  --port "${PORT}" \
  --out-dir "${OUT_DIR}"
