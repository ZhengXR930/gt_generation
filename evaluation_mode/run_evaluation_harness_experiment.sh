#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
bash "${ROOT_DIR}/evaluation_mode/reasoning/openhands/apply_zero_tool_probe_patch.sh"

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
PROBE_ROOT="${PROBE_ROOT:-${ROOT_DIR}/probe_results}"
mkdir -p "${SERVER_DIR}"

export PYTHONPATH="${ROOT_DIR}/evaluation_mode:${ROOT_DIR}/external/cybergym/src${PYTHONPATH:+:${PYTHONPATH}}"
export OPENHANDS_HARNESS_MODE=evaluation
export OPENHANDS_EVAL_PROBING="${OPENHANDS_EVAL_PROBING:-1}"
export CYBERGYM_PREEXTRACT_REPO_TAR="${CYBERGYM_PREEXTRACT_REPO_TAR:-1}"
export OPENHANDS_RUNTIME_CONTAINER_IMAGE="${OPENHANDS_RUNTIME_CONTAINER_IMAGE:-cybergym-openhands-runtime:0.33-skip-root-chown}"
export OPENHANDS_NATIVE_TOOL_CALLING="${OPENHANDS_NATIVE_TOOL_CALLING:-true}"
export MODEL="${MODEL:-deepseek/deepseek-chat}"
export MAX_ITER="${MAX_ITER:-100}"
export TIMEOUT="${TIMEOUT:-1800}"
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
  task_slug="${task_id//:/_}"
  probe_dir="${PROBE_ROOT}/${task_slug}"
  probe_path="${probe_dir}/assertion_probes.json"
  gt_dir="${ROOT_DIR}/gt_results/${task_slug}"
  assertions_path="${gt_dir}/verified_assertions.json"
  invariants_path="${gt_dir}/verified_invariants.json"
  ground_truth_path="${gt_dir}/ground_truth.json"
  reachability_path="${gt_dir}/reachability_report.json"
  sample_info_path="${gt_dir}/sample_info.json"
  default_crash_trace_path="${gt_dir}/default_crash_trace.txt"
  probe_inputs=(
    "${assertions_path}"
    "${invariants_path}"
    "${ground_truth_path}"
    "${reachability_path}"
    "${sample_info_path}"
    "${default_crash_trace_path}"
  )
  probes_stale=0
  probe_inputs_ready=1
  for probe_input in "${probe_inputs[@]}"; do
    if [[ ! -f "${probe_input}" ]]; then
      probe_inputs_ready=0
    elif [[ -f "${probe_path}" && "${probe_input}" -nt "${probe_path}" ]]; then
      probes_stale=1
    fi
  done
  if [[ -n "${QUESTIONING_AGENT_COMMAND:-}" && "${probe_inputs_ready}" -eq 1 ]]; then
    echo "[questioning-agent] ${task_id}"
    mkdir -p "${probe_dir}"
    "${PYTHON_BIN}" -m reasoning.questions \
      --assertions "${assertions_path}" \
      --invariants "${invariants_path}" \
      --ground-truth "${ground_truth_path}" \
      --reachability "${reachability_path}" \
      --sample-info "${sample_info_path}" \
      --default-crash-trace "${default_crash_trace_path}" \
      --agent-command "${QUESTIONING_AGENT_COMMAND}" \
      --role-file "${ROOT_DIR}/evaluation_mode/reasoning/questioning_agent.md" \
      --out "${probe_path}"
    probes_stale=0
  fi
  if [[ -f "${probe_path}" && "${probes_stale}" -eq 0 && "${probe_inputs_ready}" -eq 1 ]]; then
    export OPENHANDS_EVAL_PROBING=1
    export OPENHANDS_EVAL_PROBES_PATH="${probe_path}"
  else
    unset OPENHANDS_EVAL_PROBES_PATH
    if [[ "${probes_stale}" -eq 1 ]]; then
      echo "[probe] disabled ${task_id}: verified assertions are newer than ${probe_path}"
    elif [[ "${probe_inputs_ready}" -eq 0 ]]; then
      echo "[probe] disabled ${task_id}: completed GT package or public crash context is missing"
    else
      echo "[probe] disabled ${task_id}: provide QUESTIONING_AGENT_COMMAND to render ${probe_path}"
    fi
  fi
  set +e
  TASK_ID="${task_id}" evaluation_mode/run_subject_agent.sh
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
  find "${OUT_DIR}/tmp" -type d -name .git -prune -exec rm -rf {} + 2>/dev/null || true
done

echo "[done] ${OUT_DIR}"
