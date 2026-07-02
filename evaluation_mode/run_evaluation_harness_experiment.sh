#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if [[ -f "${ROOT_DIR}/config.txt" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/config.txt"
  set +a
fi

PYTHON_BIN="${CYBERGYM_PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${ROOT_DIR}/external/OpenHands/.venv/bin/python" ]]; then
    PYTHON_BIN="${ROOT_DIR}/external/OpenHands/.venv/bin/python"
  elif command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3.12)"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi

SERVER_BIND_HOST="${SERVER_BIND_HOST:-0.0.0.0}"
SERVER_READY_HOST="${SERVER_READY_HOST:-127.0.0.1}"
SERVER_CLIENT_HOST="${SERVER_CLIENT_HOST:-host.docker.internal}"
SERVER_PORT="${SERVER_PORT:-$("${PYTHON_BIN}" - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])
PY
)}"

RUN_TAG="${RUN_TAG:-evaluation_harness_$(date +%Y%m%d_%H%M%S)}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/openhands_cybergym_runs/${RUN_TAG}}"
SERVER_DIR="${OUT_DIR}/cybergym_server"
mkdir -p "${SERVER_DIR}"

export PYTHONPATH="${ROOT_DIR}/shared:${ROOT_DIR}/evaluation_mode:${ROOT_DIR}/external/cybergym/src${PYTHONPATH:+:${PYTHONPATH}}"
. "${ROOT_DIR}/shared/harness_mode_env.sh" evaluation
export MODEL="${MODEL:-deepseek/deepseek-chat}"
export MAX_ITER="${MAX_ITER:-100}"
export TIMEOUT="${TIMEOUT:-1800}"
export ENABLE_EXTERNAL_INTERPRETER="${ENABLE_EXTERNAL_INTERPRETER:-1}"
export KEEP_TMP="${KEEP_TMP:-0}"
export OUT_DIR
export SERVER_IP="${SERVER_CLIENT_HOST}"
export SERVER_PORT

TASKS=("$@")
if [[ "${#TASKS[@]}" -eq 0 ]]; then
  TASKS=("arvo:13730")
fi

SERVER_LOG="${SERVER_DIR}/server_stdout.log"
"${PYTHON_BIN}" -m cybergym.server \
  --host "${SERVER_BIND_HOST}" \
  --port "${SERVER_PORT}" \
  --log_dir "${SERVER_DIR}/logs" \
  --db_path "${SERVER_DIR}/poc.db" \
  --rate_limit_max_requests 1000 \
  --rate_limit_window_seconds 60 \
  >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!
trap 'kill "${SERVER_PID}" >/dev/null 2>&1 || true' EXIT

"${PYTHON_BIN}" - <<PY
import socket, time, sys
host="${SERVER_READY_HOST}"; port=int("${SERVER_PORT}")
deadline=time.time()+30
while time.time()<deadline:
    try:
        with socket.create_connection((host, port), timeout=1):
            sys.exit(0)
    except OSError:
        time.sleep(0.5)
print("CyberGym server did not become ready", file=sys.stderr)
sys.exit(1)
PY

SUMMARY="${OUT_DIR}/evaluation_harness_summary.jsonl"
: > "${SUMMARY}"

for task_id in "${TASKS[@]}"; do
  echo "[run] ${task_id}"
  set +e
  TASK_ID="${task_id}" shared/run_cybergym_openhands_deepseek.sh
  rc=$?
  set -e
  "${PYTHON_BIN}" - <<PY
import json, pathlib, time
out = pathlib.Path("${OUT_DIR}")
logs = sorted((out / "logs").glob("${task_id//:/_}-*"), key=lambda p: p.stat().st_mtime)
record = {
    "task_id": "${task_id}",
    "returncode": ${rc},
    "latest_log_dir": str(logs[-1]) if logs else "",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
}
with open("${SUMMARY}", "a", encoding="utf-8") as f:
    f.write(json.dumps(record, ensure_ascii=False) + "\\n")
print(json.dumps(record, indent=2, ensure_ascii=False))
PY
  if [[ "${ENABLE_EXTERNAL_INTERPRETER}" == "1" ]]; then
    task_slug="${task_id//:/_}"
    latest_log_dir="$(find "${OUT_DIR}/logs" -maxdepth 1 -type d -name "${task_slug}-*" -print 2>/dev/null | sort | tail -n 1 || true)"
    trajectory="${latest_log_dir}/trajectory"
    if [[ -n "${latest_log_dir}" && -f "${trajectory}" ]]; then
      sample_id="${task_slug}"
      gt_path="${ROOT_DIR}/gt_results/${sample_id}/ground_truth.json"
      interpreter_args=(
        --trajectory "${trajectory}"
        --out-dir "${latest_log_dir}/external_interpreter"
      )
      if [[ -f "${gt_path}" ]]; then
        interpreter_args+=(--gt "${gt_path}")
      fi
      echo "[external-interpreter] ${task_id}"
      PYTHONPATH="${ROOT_DIR}/evaluation_mode:${ROOT_DIR}/shared:${PYTHONPATH:-}" "${PYTHON_BIN}" -m external_interpreter.cli "${interpreter_args[@]}" \
        > "${latest_log_dir}/external_interpreter_stdout.json"
    else
      echo "[external-interpreter] skipped ${task_id}: trajectory not found"
    fi
  fi
  find "${OUT_DIR}/tmp" -type d -name .git -prune -exec rm -rf {} + 2>/dev/null || true
done

echo "[done] ${OUT_DIR}"
