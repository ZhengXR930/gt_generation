#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
OPENHANDS_DIR="${ROOT_DIR}/external/OpenHands"
PATCH_FILE="${ROOT_DIR}/evaluation_mode/reasoning/openhands/zero_tool_probe.patch"
CONTROLLER="${OPENHANDS_DIR}/openhands/controller/agent_controller.py"
CODEACT="${OPENHANDS_DIR}/openhands/agenthub/codeact_agent/codeact_agent.py"

if [[ ! -f "${CONTROLLER}" || ! -f "${CODEACT}" ]]; then
  echo "OpenHands checkout is missing under ${OPENHANDS_DIR}" >&2
  exit 2
fi

# The vendor checkout is intentionally ignored by the parent repository. Keep the
# integration reproducible by applying the tracked patch once and treating the two
# phase markers as the idempotence check.
if grep -q "EVALUATION_PROBE_MARKER" "${CONTROLLER}" \
  && grep -q "_tool_free_probe_actions" "${CODEACT}"; then
  exit 0
fi

if ! patch --dry-run --forward -p1 -d "${OPENHANDS_DIR}" < "${PATCH_FILE}" >/dev/null; then
  echo "OpenHands 0.33 checkout does not match the expected harness base; probe patch was not applied" >&2
  exit 1
fi

patch --forward -p1 -d "${OPENHANDS_DIR}" < "${PATCH_FILE}"
