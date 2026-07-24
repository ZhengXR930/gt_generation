#!/usr/bin/env bash
set -euo pipefail

# coco (Trae CLI) adapter for runner.py's GT_AGENT_COMMAND contract.
# Bridges runner args (--role-file/--sample/--result-dir) to a non-interactive
# `coco exec` session that executes one GT-generator stage and writes its
# artifacts. Mirrors the codex adapter: coco exec takes the prompt positionally,
# -m selects the model, -y bypasses approvals/sandbox (host is the sandbox).
#
#   export GT_AGENT_COMMAND="$(pwd)/gt_generation/adapters/coco/gt_agent_coco.sh"
#   export GT_AGENT_MODEL="gpt-5.5"        # any id from `coco models`
#   python3 gt_generation/runner.py ...
#
# One model for every stage: no per-stage escalation (see gt_plugin.py).

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

LOG="${RESULT_DIR}/coco_stage_$(basename "${ROLE_FILE%.md}").log"
# coco exec has no --cd flag; set the working directory in the shell instead.
cd "$REPO_ROOT"

COCO_ARGS=(exec -y)
if [[ -n "${GT_AGENT_MODEL:-}" ]]; then
  COCO_ARGS+=(-m "$GT_AGENT_MODEL")
fi
COCO_ARGS+=("$PROMPT")

set +e
coco "${COCO_ARGS[@]}" 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e
exit "$rc"
