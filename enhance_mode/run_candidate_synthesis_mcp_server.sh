#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x "$ROOT/external/OpenHands/.venv/bin/python" ]; then
  PYTHON_BIN="$ROOT/external/OpenHands/.venv/bin/python"
fi

export PYTHONPATH="$ROOT/enhance_mode:$ROOT/shared${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" -m enhance_mcp_servers.candidate_synthesis_server "$@"
