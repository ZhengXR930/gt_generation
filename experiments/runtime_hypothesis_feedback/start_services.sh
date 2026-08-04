#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$HERE/../.." && pwd)
OH_PY=${OPENHANDS_PYTHON:-/home/xinran/.cache/pypoetry/virtualenvs/openhands-ai-pW2ZHCQY-py3.12/bin/python}

mkdir -p "$HERE/server/logs" "$HERE/feedback_logs" "$HERE/runs"

# Condition B is contacted directly from the OpenHands Docker container, so
# its service must be reachable through the Docker host bridge.
PYTHONPATH="$REPO_ROOT/external/cybergym/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$OH_PY" -m cybergym.server \
    --host 0.0.0.0 \
    --port 8766 \
    --db_path "$HERE/server/poc.db" \
    --log_dir "$HERE/server/logs" \
    >"$HERE/runs/cybergym_server.log" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$HERE/runs/cybergym_server.pid"

HYPOTHESIS_UPSTREAM=http://127.0.0.1:8766 \
PYTHONPATH="$REPO_ROOT:$REPO_ROOT/external/cybergym/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$OH_PY" -m uvicorn \
    experiments.runtime_hypothesis_feedback.feedback_proxy:app \
    --host 0.0.0.0 \
    --port 8767 \
    >"$HERE/runs/feedback_proxy.log" 2>&1 &
PROXY_PID=$!
echo "$PROXY_PID" > "$HERE/runs/feedback_proxy.pid"

cleanup() {
  kill "$PROXY_PID" "$SERVER_PID" 2>/dev/null || true
  wait "$PROXY_PID" "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "CyberGym server PID $SERVER_PID on 0.0.0.0:8766"
echo "Feedback proxy PID $PROXY_PID on 0.0.0.0:8767"
wait "$SERVER_PID" "$PROXY_PID"
