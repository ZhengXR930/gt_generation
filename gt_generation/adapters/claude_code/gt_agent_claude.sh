#!/usr/bin/env bash
set -euo pipefail

# claude_code adapter for runner.py's GT_AGENT_COMMAND.
# Bridges runner args (--role-file/--sample/--result-dir) to a headless `claude -p`
# session that executes ONE pipeline role autonomously and writes its artifacts.
#
#   export GT_AGENT_COMMAND="$(pwd)/gt_generation/adapters/claude_code/gt_agent_claude.sh"
#   python3 gt_generation/runner.py ...
#
# Env knobs: GT_CLAUDE_MODEL, GT_CLAUDE_MAX_TURNS (default 60).

ROLE_FILE="" ; SAMPLE="" ; RESULT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role-file) ROLE_FILE="$2"; shift 2 ;;
    --sample) SAMPLE="$2"; shift 2 ;;
    --result-dir) RESULT_DIR="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -z "$ROLE_FILE" || -z "$SAMPLE" || -z "$RESULT_DIR" ]] && {
  echo "usage: $0 --role-file ROLE.md --sample sample.json --result-dir DIR" >&2; exit 2; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CONTRACT="${REPO_ROOT}/gt_generation/roles/AGENT_CONTRACT.md"
mkdir -p "$RESULT_DIR"

PROMPT="$(cat <<EOF
You are executing ONE stage of a fine-grained memory-safety GT-generation pipeline,
headless and autonomously. Follow the role instructions exactly and write every
required artifact into the result directory. Use gt_toolkit via:
  PYTHONPATH=${REPO_ROOT}/gt_generation python3 -m gt_toolkit ...

$( [[ -f "$CONTRACT" ]] && printf 'Global contract:\n%s\n' "$(cat "$CONTRACT")" )

Role instructions:
$(cat "$ROLE_FILE")

Sample metadata file: $SAMPLE
$( [[ -f "$SAMPLE" ]] && printf '%s\n' "$(cat "$SAMPLE")" )

Result directory (write outputs here): $RESULT_DIR

When done, print a one-line JSON: {"stage_ok": true/false, "outputs": [...]}.
EOF
)"

LOG="${RESULT_DIR}/claude_stage_$(basename "${ROLE_FILE%.md}").log"
cd "$REPO_ROOT"
set +e
claude -p "$PROMPT" \
  --allowedTools "Bash Read Write Edit Glob Grep" \
  --add-dir "$RESULT_DIR" \
  --dangerously-skip-permissions \
  --max-turns "${GT_CLAUDE_MAX_TURNS:-60}" \
  ${GT_CLAUDE_MODEL:+--model "$GT_CLAUDE_MODEL"} \
  2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e

# Disk hygiene backstop: drop the working source copy and any .git tree under the
# result dir (the biggest disk hogs). Container cleanup is the role's job via
# `docker run --rm` / `docker rm`, so we don't blanket-prune others' containers here.
rm -rf "${RESULT_DIR}/_work" 2>/dev/null || true
find "${RESULT_DIR}" -name .git -type d -prune -exec rm -rf {} + 2>/dev/null || true

exit "$rc"
