#!/usr/bin/env bash
set -euo pipefail

# Run the CyberGym subject agent for evaluation.
#
# Required environment:
#   DEEPSEEK_API_KEY or LLM_API_KEY
#   CYBERGYM_DATA_DIR   path to cybergym_data/data
#   SERVER_IP           CyberGym server host
#   SERVER_PORT         CyberGym server port
#   TASK_ID             e.g. arvo:10400
#
# Optional environment:
#   OUT_DIR             default: ./openhands_cybergym_runs
#   MODEL               default: deepseek/deepseek-chat
#   DEEPSEEK_BASE_URL   default: https://api.deepseek.com
#   DIFFICULTY          default: level1
#   MAX_ITER            default: 100
#   TIMEOUT             default: 1200
#   KEEP_TMP            default: 0; set to 1 only for workspace debugging
#   OPENHANDS_NATIVE_TOOL_CALLING optional true/false override for OpenHands tool calling mode

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OPENHANDS_REPO="${OPENHANDS_REPO:-${ROOT_DIR}/external/OpenHands}"
OPENHANDS_SETUP="${ROOT_DIR}/scripts/setup_openhands.sh"
ADAPTER="${ROOT_DIR}/external/cybergym/examples/agents/openhands/run.py"
DEFAULT_CYBERGYM_DATA_DIR="${ROOT_DIR}/external/cybergym_data_subset/data"

MODEL="${MODEL:-deepseek/deepseek-chat}"
DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.deepseek.com}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/openhands_cybergym_runs}"
CYBERGYM_DATA_DIR="${CYBERGYM_DATA_DIR:-${DEFAULT_CYBERGYM_DATA_DIR}}"
DIFFICULTY="${DIFFICULTY:-level1}"
MAX_ITER="${MAX_ITER:-100}"
TIMEOUT="${TIMEOUT:-1200}"
KEEP_TMP="${KEEP_TMP:-0}"
OPENHANDS_RUNTIME_READY_TIMEOUT="${OPENHANDS_RUNTIME_READY_TIMEOUT:-300}"
PYTHON_BIN="${CYBERGYM_PYTHON:-$(command -v python3.12 || command -v python3)}"

if [[ ! -f "${OPENHANDS_REPO}/pyproject.toml" ]]; then
  echo "OpenHands 0.33.0 is not installed; bootstrapping it now." >&2
  "${OPENHANDS_SETUP}"
fi

: "${TASK_ID:?Set TASK_ID, e.g. arvo:10400}"
: "${CYBERGYM_DATA_DIR:?Set CYBERGYM_DATA_DIR to cybergym_data/data}"
: "${SERVER_IP:?Set SERVER_IP for the CyberGym server}"
: "${SERVER_PORT:?Set SERVER_PORT for the CyberGym server}"

if [[ -z "${DEEPSEEK_API_KEY:-}" && -z "${LLM_API_KEY:-}" ]]; then
  echo "Set DEEPSEEK_API_KEY or LLM_API_KEY before running." >&2
  exit 2
fi

export PATH="${HOME}/Library/Python/3.12/bin:${HOME}/.local/bin:${PATH}"
export PYTHONPATH="${ROOT_DIR}:${ROOT_DIR}/external/cybergym/src${PYTHONPATH:+:${PYTHONPATH}}"
export LLM_API_KEY="${DEEPSEEK_API_KEY:-${LLM_API_KEY:-}}"
export OPENHANDS_HARNESS_MODE="${OPENHANDS_HARNESS_MODE:-evaluation}"
export CYBERGYM_PREEXTRACT_REPO_TAR="${CYBERGYM_PREEXTRACT_REPO_TAR:-1}"
export OPENHANDS_RUNTIME_CONTAINER_IMAGE="${OPENHANDS_RUNTIME_CONTAINER_IMAGE:-cybergym-openhands-runtime:0.33-skip-root-chown}"

mkdir -p "${OUT_DIR}/logs" "${OUT_DIR}/tmp"
OPENHANDS_DOCKER_CONFIG="${OPENHANDS_DOCKER_CONFIG:-${OUT_DIR}/docker_config_no_creds}"
mkdir -p "${OPENHANDS_DOCKER_CONFIG}"
if [[ ! -f "${OPENHANDS_DOCKER_CONFIG}/config.json" ]]; then
  printf '{"auths":{}}\n' > "${OPENHANDS_DOCKER_CONFIG}/config.json"
fi
export DOCKER_CONFIG="${OPENHANDS_DOCKER_CONFIG}"
export OPENHANDS_RUNTIME_READY_TIMEOUT

EXTRA_OPENHANDS_ARGS=()
if [[ -n "${OPENHANDS_NATIVE_TOOL_CALLING:-}" ]]; then
  EXTRA_OPENHANDS_ARGS+=(--native_tool_calling "${OPENHANDS_NATIVE_TOOL_CALLING}")
fi

"${PYTHON_BIN}" "${ADAPTER}" \
  --model "${MODEL}" \
  --base_url "${DEEPSEEK_BASE_URL}" \
  --repo "${OPENHANDS_REPO}" \
  --log_dir "${OUT_DIR}/logs" \
  --tmp_dir "${OUT_DIR}/tmp" \
  --data_dir "${CYBERGYM_DATA_DIR}" \
  --task_id "${TASK_ID}" \
  --server "http://${SERVER_IP}:${SERVER_PORT}" \
  --timeout "${TIMEOUT}" \
  --max_iter "${MAX_ITER}" \
  "${EXTRA_OPENHANDS_ARGS[@]+"${EXTRA_OPENHANDS_ARGS[@]}"}" \
  --remove_tmp "$([[ "${KEEP_TMP}" == "1" ]] && echo false || echo true)" \
  --silent false \
  --difficulty "${DIFFICULTY}"
