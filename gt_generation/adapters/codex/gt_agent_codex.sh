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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
mkdir -p "$RESULT_DIR"

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
if [[ -n "${GT_AGENT_MODEL:-}" ]]; then
  CODEX_ARGS+=(-m "$GT_AGENT_MODEL")
fi
CODEX_ARGS+=("$PROMPT")

exec codex "${CODEX_ARGS[@]}"
