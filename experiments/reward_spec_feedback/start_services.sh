#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$HERE/../.." && pwd)
OH_PY=${OPENHANDS_PYTHON:-/home/xinran/.cache/pypoetry/virtualenvs/openhands-ai-pW2ZHCQY-py3.12/bin/python}

mkdir -p "$HERE/server/logs" "$HERE/feedback_logs" "$HERE/runs"

PYTHONPATH="$REPO_ROOT/external/cybergym/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$OH_PY" -m cybergym.server \
    --host 0.0.0.0 --port 8766 \
    --db_path "$HERE/server/poc.db" \
    --log_dir "$HERE/server/logs" \
    >"$HERE/runs/cybergym_server.log" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$HERE/runs/cybergym_server.pid"

REWARD_SPEC_UPSTREAM=http://127.0.0.1:8766 \
REWARD_SPEC_ROOT="$REPO_ROOT/experiments/reward_spec_schema_study/final_results" \
PYTHONPATH="$REPO_ROOT:$REPO_ROOT/external/cybergym/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$OH_PY" -m uvicorn experiments.reward_spec_feedback.proxy:app \
    --host 0.0.0.0 --port 8767 \
    >"$HERE/runs/reward_spec_proxy.log" 2>&1 &
PROXY_PID=$!
echo "$PROXY_PID" > "$HERE/runs/reward_spec_proxy.pid"

cleanup() {
  kill "$PROXY_PID" "$SERVER_PID" 2>/dev/null || true
  wait "$PROXY_PID" "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "CyberGym server PID $SERVER_PID on 0.0.0.0:8766"
echo "Reward-Spec proxy PID $PROXY_PID on 0.0.0.0:8767"
wait "$SERVER_PID" "$PROXY_PID"
