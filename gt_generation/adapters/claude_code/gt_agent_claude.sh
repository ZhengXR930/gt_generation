#!/usr/bin/env bash
set -euo pipefail

# claude_code adapter for runner.py's GT_AGENT_COMMAND.
# Bridges runner args (--role-file/--sample/--result-dir) to a headless `claude -p`
# session that executes the complete GT-generator role and writes its artifacts.
#
#   export GT_AGENT_COMMAND="$(pwd)/gt_generation/adapters/claude_code/gt_agent_claude.sh"
#   python3 gt_generation/runner.py ...
#
# Env knobs: GT_CLAUDE_MODEL (defaults to the faster `sonnet`) and
# GT_CLAUDE_EFFORT. Set GT_CLAUDE_MODEL=opus explicitly for an exceptional
# sample that needs escalation. Claude's JSON result is retained in the stage
# log so duration, turns, and model usage are auditable.

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
mkdir -p "$RESULT_DIR"
SAMPLE_CONTENT=""
if [[ -f "$SAMPLE" ]]; then
  SAMPLE_CONTENT="$(< "$SAMPLE")"
fi
ROLE_CONTENT="$(< "$ROLE_FILE")"

printf -v PROMPT '%s\n' \
  'You are executing one isolated GT harness stage, headless and autonomously.' \
  "Follow this stage's role instructions exactly and write only its required artifacts" \
  'into the result directory. No conversational state from another stage is available.' \
  'Run long shell commands synchronously with the largest Bash timeout available.' \
  'Never leave a command in the background or end the session while work is pending.' \
  'Use gt_toolkit via:' \
  "  PYTHONPATH=${REPO_ROOT}/gt_generation python3 -m gt_toolkit ..." \
  '' \
  'Role instructions:' \
  "$ROLE_CONTENT" \
  '' \
  "Sample metadata file: $SAMPLE" \
  "$SAMPLE_CONTENT" \
  '' \
  "Result directory (write outputs here): $RESULT_DIR" \
  '' \
  'When done, print a one-line JSON: {"stage_ok": true/false, "outputs": [...]}.'

LOG="${RESULT_DIR}/claude_stage_$(basename "${ROLE_FILE%.md}").log"
cd "$REPO_ROOT"
CLAUDE_ARGS=(
  -p "$PROMPT"
  --allowedTools "Bash Read Write Edit Glob Grep"
  --add-dir "$RESULT_DIR"
  --dangerously-skip-permissions
  --no-session-persistence
  --output-format json
)
MODEL="${GT_CLAUDE_MODEL:-sonnet}"
case "$(basename "${ROLE_FILE%.md}")" in
  02_*|03_*|04_*) MODEL="${GT_CLAUDE_COMPLEX_MODEL:-claude-opus-4-6}" ;;
esac
CLAUDE_ARGS+=(--model "$MODEL")
if [[ -n "${GT_CLAUDE_EFFORT:-}" ]]; then
  CLAUDE_ARGS+=(--effort "$GT_CLAUDE_EFFORT")
fi
set +e
claude "${CLAUDE_ARGS[@]}" 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e

exit "$rc"
