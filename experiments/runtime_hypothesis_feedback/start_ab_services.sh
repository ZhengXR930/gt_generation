#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$HERE/../.." && pwd)
OH_PY=${OPENHANDS_PYTHON:-/home/xinran/.cache/pypoetry/virtualenvs/openhands-ai-pW2ZHCQY-py3.12/bin/python}

mkdir -p "$HERE/server/logs" "$HERE/feedback_logs" "$HERE/runs"

PYTHONPATH="$REPO_ROOT/external/cybergym/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$OH_PY" -m cybergym.server \
    --host 0.0.0.0 \
    --port 8766 \
    --db_path "$HERE/server/poc.db" \
    --log_dir "$HERE/server/logs" \
    >"$HERE/runs/ab_cybergym_server.log" 2>&1 &
SERVER_PID=$!

HYPOTHESIS_UPSTREAM=http://127.0.0.1:8766 \
HYPOTHESIS_REWARD_PROTOCOL=v6 \
PYTHONPATH="$REPO_ROOT:$REPO_ROOT/external/cybergym/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$OH_PY" -m uvicorn \
    experiments.runtime_hypothesis_feedback.feedback_proxy:app \
    --host 0.0.0.0 \
    --port 8767 \
    >"$HERE/runs/ab_feedback_v6.log" 2>&1 &
V6_PID=$!

HYPOTHESIS_UPSTREAM=http://127.0.0.1:8766 \
HYPOTHESIS_REWARD_PROTOCOL=v7 \
PYTHONPATH="$REPO_ROOT:$REPO_ROOT/external/cybergym/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$OH_PY" -m uvicorn \
    experiments.runtime_hypothesis_feedback.feedback_proxy:app \
    --host 0.0.0.0 \
    --port 8768 \
    >"$HERE/runs/ab_feedback_v7.log" 2>&1 &
V7_PID=$!

printf '%s\n' "$SERVER_PID" >"$HERE/runs/ab_cybergym_server.pid"
printf '%s\n' "$V6_PID" >"$HERE/runs/ab_feedback_v6.pid"
printf '%s\n' "$V7_PID" >"$HERE/runs/ab_feedback_v7.pid"

cleanup() {
  kill "$V7_PID" "$V6_PID" "$SERVER_PID" 2>/dev/null || true
  wait "$V7_PID" "$V6_PID" "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "CyberGym upstream PID $SERVER_PID on 8766"
echo "V6 feedback PID $V6_PID on 8767"
echo "V7 feedback PID $V7_PID on 8768"
wait "$SERVER_PID" "$V6_PID" "$V7_PID"
