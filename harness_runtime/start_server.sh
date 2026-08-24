#!/usr/bin/env bash
# Start the local CyberGym validation server used by all PoC harnesses.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ROOT/.." && pwd)"

export PYTHONPATH="$REPO_ROOT/external/cybergym/src${PYTHONPATH:+:$PYTHONPATH}"
PYTHON_BIN="${PYTHON:-python3}"

can_run_server() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
if sys.version_info < (3, 10):
    raise SystemExit(1)
import cybergym.server.__main__
PY
}

if ! can_run_server "$PYTHON_BIN"
then
  for candidate in "$HOME"/.cache/pypoetry/virtualenvs/openhands-ai-*/bin/python; do
    if [ -x "$candidate" ] && can_run_server "$candidate"
    then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi

if ! can_run_server "$PYTHON_BIN"; then
  echo "No Python >=3.10 environment can run cybergym.server" >&2
  exit 1
fi

mkdir -p "$ROOT/server/logs"

exec "$PYTHON_BIN" -m cybergym.server \
  --host 0.0.0.0 \
  --port 8666 \
  --db_path "$ROOT/server/poc.db" \
  --log_dir "$ROOT/server/logs"
