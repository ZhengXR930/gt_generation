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

RUN_TAG="${RUN_TAG:-enhancement_harness_$(date +%Y%m%d_%H%M%S)}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/openhands_cybergym_runs/${RUN_TAG}}"
SERVER_DIR="${OUT_DIR}/cybergym_server"
mkdir -p "${SERVER_DIR}"

export PYTHONPATH="${ROOT_DIR}/shared:${ROOT_DIR}/enhance_mode:${ROOT_DIR}/external/cybergym/src${PYTHONPATH:+:${PYTHONPATH}}"
. "${ROOT_DIR}/shared/harness_mode_env.sh" enhancement
export CANDIDATE_SYNTHESIS_SUBMIT_SERVER="http://127.0.0.1:${SERVER_PORT}"
export MODEL="${MODEL:-deepseek/deepseek-chat}"
export MAX_ITER="${MAX_ITER:-100}"
export TIMEOUT="${TIMEOUT:-1800}"
export KEEP_TMP="${KEEP_TMP:-1}"
export RUN_ATTEMPTS="${RUN_ATTEMPTS:-3}"
export OUT_DIR
export SERVER_IP="${SERVER_CLIENT_HOST}"
export SERVER_PORT

TASKS=("$@")
if [[ "${#TASKS[@]}" -eq 0 ]]; then
  TASKS=("arvo:12466" "arvo:1304" "arvo:13704")
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

SUMMARY="${OUT_DIR}/enhancement_harness_summary.jsonl"
: > "${SUMMARY}"

for task_id in "${TASKS[@]}"; do
  echo "[run] ${task_id}"
  unset CANDIDATE_SYNTHESIS_REACHABILITY_DEBUG_COMMAND
  unset CANDIDATE_SYNTHESIS_REACHABILITY_COVERAGE_COMMAND
  unset CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND
  eval "$("${PYTHON_BIN}" - <<PY
import json, pathlib, shlex

root = pathlib.Path("${ROOT_DIR}")
task_id = "${task_id}"
sample_id = task_id.replace(":", "_")
sample_dir = root / "gt_results" / sample_id
trigger_path = sample_dir / "trigger.json"
state_path = sample_dir / "sample_state.json"
runner_path = sample_dir / "enhancement_runner.json"

exports = []
if runner_path.exists():
    try:
        runner = json.loads(runner_path.read_text(encoding="utf-8", errors="replace"))
        if runner.get("debug_command"):
            exports.append(("CANDIDATE_SYNTHESIS_REACHABILITY_DEBUG_COMMAND", str(runner["debug_command"])))
        if runner.get("coverage_command"):
            exports.append(("CANDIDATE_SYNTHESIS_REACHABILITY_COVERAGE_COMMAND", str(runner["coverage_command"])))
        if runner.get("sanitizer_command"):
            exports.append(("CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND", str(runner["sanitizer_command"])))
    except json.JSONDecodeError:
        pass

container_image = ""
target_command = ""
if trigger_path.exists():
    try:
        trigger = json.loads(trigger_path.read_text(encoding="utf-8", errors="replace"))
        container_image = str(trigger.get("container_image") or "")
        target_command = str(trigger.get("target_command") or "")
    except json.JSONDecodeError:
        pass
if not container_image:
    suffix = task_id.split(":", 1)[1] if ":" in task_id else sample_id.removeprefix("arvo_")
    container_image = f"n132/arvo:{suffix}-vul"
if not target_command and state_path.exists():
    try:
        state = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        target_command = str((state.get("reproduction") or {}).get("target_command") or "")
    except json.JSONDecodeError:
        pass
if target_command:
    docker_cmd = (
        "docker run --rm --platform linux/amd64 "
        "-v {candidate_path}:/tmp/poc:ro "
        f"{shlex.quote(container_image)} /bin/bash -lc {shlex.quote(target_command)}"
    )
    if not any(key == "CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND" for key, _ in exports):
        exports.append(("CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND", docker_cmd))

for key, value in exports:
    print(f"export {key}={shlex.quote(value)}")
PY
)"
  if [[ -n "${CANDIDATE_SYNTHESIS_REACHABILITY_DEBUG_COMMAND:-}" ]]; then
    echo "[hypothesis-debug] configured"
  else
    echo "[hypothesis-debug] not configured for ${task_id}; H1-H4 feedback will fail fast"
  fi
  if [[ -n "${CANDIDATE_SYNTHESIS_REACHABILITY_COVERAGE_COMMAND:-}" ]]; then
    echo "[hypothesis-coverage] configured"
  else
    echo "[hypothesis-coverage] not configured for ${task_id}"
  fi
  if [[ -n "${CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND:-}" ]]; then
    echo "[hypothesis-sanitizer] configured"
  else
    echo "[hypothesis-sanitizer] not configured for ${task_id}"
  fi
  rc=0
  latest_log_dir=""
  interrupted=0
  for attempt in $(seq 1 "${RUN_ATTEMPTS}"); do
    echo "[attempt] ${task_id} ${attempt}/${RUN_ATTEMPTS}"
    set +e
    TASK_ID="${task_id}" shared/run_cybergym_openhands_deepseek.sh
    rc=$?
    set -e
    latest_log_dir="$("${PYTHON_BIN}" - <<PY
import pathlib
out = pathlib.Path("${OUT_DIR}")
logs = sorted((out / "logs").glob("${task_id//:/_}-*"), key=lambda p: p.stat().st_mtime)
print(str(logs[-1]) if logs else "")
PY
)"
    interrupted=0
    if [[ -n "${latest_log_dir}" ]] && grep -R -q 'STATUS[$]ERROR_LLM_SERVICE_UNAVAILABLE' "${latest_log_dir}" 2>/dev/null; then
      interrupted=1
    fi
    if [[ "${interrupted}" != "1" ]]; then
      break
    fi
    echo "[retry] ${task_id} interrupted by LLM service error"
    docker ps -aq --filter name=openhands-runtime- | xargs -r docker rm -f >/dev/null 2>&1 || true
    find "${OUT_DIR}/tmp" -type d -name .git -prune -exec rm -rf {} + 2>/dev/null || true
    rm -rf "${OUT_DIR}/tmp"/*
    if [[ "${attempt}" == "${RUN_ATTEMPTS}" ]]; then
      break
    fi
    sleep 5
  done
  "${PYTHON_BIN}" - <<PY
import json, pathlib, time
out = pathlib.Path("${OUT_DIR}")
logs = sorted((out / "logs").glob("${task_id//:/_}-*"), key=lambda p: p.stat().st_mtime)
record = {
    "task_id": "${task_id}",
    "returncode": ${rc},
    "attempts": int("${attempt}"),
    "llm_service_interrupted": bool(int("${interrupted}")),
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
