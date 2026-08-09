#!/usr/bin/env bash
set -euo pipefail

# Codex adapter for runner.py's GT_AGENT_COMMAND contract.

ROLE_FILE=""
SAMPLE=""
RESULT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role-file) ROLE_FILE="$2"; shift 2 ;;
    --sample) SAMPLE="$2"; shift 2 ;;
    --result-dir) RESULT_DIR="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$ROLE_FILE" || -z "$SAMPLE" || -z "$RESULT_DIR" ]]; then
  echo "usage: $0 --role-file ROLE.md --sample sample.json --result-dir DIR" >&2
  exit 2
fi

CODEX_ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$CODEX_ADAPTER_DIR/../../.." && pwd)"
mkdir -p "$RESULT_DIR"

load_config_key() {
  local name="$1"
  local value=""
  if [[ -f "$REPO_ROOT/config.txt" ]]; then
    value="$(grep -E "^${name}=" "$REPO_ROOT/config.txt" | head -1 | cut -d= -f2- | tr -d '"' | tr -d '[:space:]')"
  fi
  [[ -n "$value" ]] && printf '%s' "$value"
}

# Auth: the default path uses the official OpenAI API. A config may instead
# provide GT_CODEX_PROVIDER_* to route codex through a custom provider.
if [[ -z "${GT_CODEX_PROVIDER_ID:-}" && -z "${OPENAI_API_KEY:-}" && -f "$REPO_ROOT/config.txt" ]]; then
  key="$(load_config_key "OPENAI_API_KEY_OFFICIAL")"
  [[ -n "$key" ]] && export OPENAI_API_KEY="$key"
fi
if [[ -n "${GT_CODEX_PROVIDER_ID:-}" && -n "${GT_CODEX_PROVIDER_ENV_KEY:-}" && -z "${!GT_CODEX_PROVIDER_ENV_KEY:-}" ]]; then
  key="$(load_config_key "$GT_CODEX_PROVIDER_ENV_KEY")"
  [[ -n "$key" ]] && export "$GT_CODEX_PROVIDER_ENV_KEY=$key"
fi
if [[ -n "${GT_CODEX_PROVIDER_ID:-}" ]]; then
  CODEX_PROVIDER_BASE_URL="${GT_CODEX_PROVIDER_BASE_URL:-}"
  if [[ -n "${GT_CODEX_PROVIDER_ENV_KEY:-}" && -n "${!GT_CODEX_PROVIDER_ENV_KEY:-}" ]]; then
    placeholder="\${${GT_CODEX_PROVIDER_ENV_KEY}}"
    CODEX_PROVIDER_BASE_URL="${CODEX_PROVIDER_BASE_URL//$placeholder/${!GT_CODEX_PROVIDER_ENV_KEY}}"
  fi
  if [[ "$CODEX_PROVIDER_BASE_URL" == *"\${${GT_CODEX_PROVIDER_ENV_KEY:-}}"* ]]; then
    echo "missing value for ${GT_CODEX_PROVIDER_ENV_KEY:-custom provider key} in env or config.txt" >&2
    exit 2
  fi
  if [[ "${GT_CODEX_PROVIDER_BRIDGE:-}" == "modelhub_crawl" ]]; then
    CODEX_BRIDGE_TARGET_URL="${GT_CODEX_PROVIDER_BRIDGE_TARGET_URL:-}"
    if [[ -z "$CODEX_BRIDGE_TARGET_URL" ]]; then
      echo "missing ModelHub bridge target URL or ${GT_CODEX_PROVIDER_ENV_KEY:-custom provider key}" >&2
      exit 2
    fi
    if [[ -n "${GT_CODEX_PROVIDER_ENV_KEY:-}" && "$CODEX_BRIDGE_TARGET_URL" == *"\${${GT_CODEX_PROVIDER_ENV_KEY}}"* && -z "${!GT_CODEX_PROVIDER_ENV_KEY:-}" ]]; then
      echo "missing ${GT_CODEX_PROVIDER_ENV_KEY} in env or config.txt" >&2
      exit 2
    fi

    BRIDGE_DIR="$RESULT_DIR/.codex_modelhub_bridge"
    mkdir -p "$BRIDGE_DIR"
    BRIDGE_PORT_FILE="$BRIDGE_DIR/port"
    BRIDGE_LOG_FILE="$BRIDGE_DIR/bridge.log"
    rm -f "$BRIDGE_PORT_FILE"
    "${GT_CODEX_BRIDGE_PYTHON:-python3}" "$CODEX_ADAPTER_DIR/modelhub_crawl_bridge.py" \
      --host 127.0.0.1 \
      --port 0 \
      --port-file "$BRIDGE_PORT_FILE" \
      --target-url "$CODEX_BRIDGE_TARGET_URL" \
      --api-key-env "${GT_CODEX_PROVIDER_ENV_KEY:-}" \
      --max-tokens "${GT_CODEX_PROVIDER_BRIDGE_MAX_TOKENS:-16384}" \
      --timeout-seconds "${GT_CODEX_PROVIDER_BRIDGE_TIMEOUT_SECONDS:-600}" \
      --log-file "$BRIDGE_LOG_FILE" \
      >>"$BRIDGE_LOG_FILE" 2>&1 &
    BRIDGE_PID="$!"
    cleanup_bridge() {
      kill "$BRIDGE_PID" >/dev/null 2>&1 || true
      wait "$BRIDGE_PID" >/dev/null 2>&1 || true
    }
    trap cleanup_bridge EXIT
    for _ in {1..100}; do
      [[ -s "$BRIDGE_PORT_FILE" ]] && break
      if ! kill -0 "$BRIDGE_PID" >/dev/null 2>&1; then
        echo "ModelHub crawl bridge exited before writing its port; see $BRIDGE_LOG_FILE" >&2
        exit 2
      fi
      sleep 0.05
    done
    if [[ ! -s "$BRIDGE_PORT_FILE" ]]; then
      echo "ModelHub crawl bridge did not become ready; see $BRIDGE_LOG_FILE" >&2
      exit 2
    fi
    BRIDGE_PORT="$(<"$BRIDGE_PORT_FILE")"
    CODEX_PROVIDER_BASE_URL="http://127.0.0.1:${BRIDGE_PORT}"
  fi
fi

PROMPT="$(<"$ROLE_FILE")

Sample metadata file: $SAMPLE
Result directory: $RESULT_DIR

Execute only this role autonomously in one isolated session. Read the sample metadata
and staged artifacts explicitly; no conversational state from another stage exists.
Write the role's required artifacts into the result directory. Do not delegate. When
complete, report which deterministic gates passed and any evidence limitations."

# One model for every stage via the generic GT_AGENT_MODEL (see gt_plugin.py);
# omit -m to let codex use its own default when unset.
CODEX_ARGS=(exec --cd "$REPO_ROOT" --dangerously-bypass-approvals-and-sandbox --ephemeral)
if [[ "${GT_CODEX_STRICT_CONFIG:-1}" == "1" ]]; then
  CODEX_ARGS+=(--strict-config)
fi
if [[ -n "${GT_CODEX_PROVIDER_ID:-}" ]]; then
  CODEX_ARGS+=(
    -c "model_provider=\"${GT_CODEX_PROVIDER_ID}\""
    -c "model_providers.${GT_CODEX_PROVIDER_ID}.name=\"${GT_CODEX_PROVIDER_NAME:-$GT_CODEX_PROVIDER_ID}\""
    -c "model_providers.${GT_CODEX_PROVIDER_ID}.base_url=\"${CODEX_PROVIDER_BASE_URL}\""
    -c "model_providers.${GT_CODEX_PROVIDER_ID}.wire_api=\"${GT_CODEX_PROVIDER_WIRE_API:-responses}\""
  )
  if [[ -n "${GT_CODEX_PROVIDER_ENV_KEY:-}" ]]; then
    CODEX_ARGS+=(-c "model_providers.${GT_CODEX_PROVIDER_ID}.env_key=\"${GT_CODEX_PROVIDER_ENV_KEY}\"")
  fi
fi
if [[ -n "${GT_AGENT_MODEL:-}" ]]; then
  CODEX_ARGS+=(-m "$GT_AGENT_MODEL")
fi
if [[ -n "${GT_AGENT_REASONING_EFFORT:-}" ]]; then
  CODEX_ARGS+=(-c "model_reasoning_effort=\"${GT_AGENT_REASONING_EFFORT}\"")
fi
CODEX_ARGS+=("$PROMPT")

set +e
codex "${CODEX_ARGS[@]}"
CODEX_STATUS=$?
set -e
exit "$CODEX_STATUS"
