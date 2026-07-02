#!/usr/bin/env bash
set -euo pipefail

# Example GT_AGENT_COMMAND adapter for runner.py (see adapters/codex/README.md).
# Replace the final command with the concrete CLI you want to use.

ROLE_FILE=""
SAMPLE=""
RESULT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role-file)
      ROLE_FILE="$2"
      shift 2
      ;;
    --sample)
      SAMPLE="$2"
      shift 2
      ;;
    --result-dir)
      RESULT_DIR="$2"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$ROLE_FILE" || -z "$SAMPLE" || -z "$RESULT_DIR" ]]; then
  echo "usage: $0 --role-file ROLE.md --sample sample.json --result-dir DIR" >&2
  exit 2
fi

mkdir -p "$RESULT_DIR"
PROMPT="$RESULT_DIR/current_role_prompt.md"
CONTRACT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/gt_generation/roles/AGENT_CONTRACT.md"
cat > "$PROMPT" <<EOF
Use the following role instructions and sample metadata.

Optional global contract:
$(cat "$CONTRACT")

Role instructions:
$(cat "$ROLE_FILE")

Sample metadata path:
$SAMPLE

Result directory:
$RESULT_DIR

You must write all required artifacts into the result directory.
EOF

echo "Prepared combined prompt at $PROMPT" >&2
echo "Replace scripts/gt_agent_wrapper_example.sh final command with your agent CLI." >&2

# Examples only; uncomment and adapt one of these for your environment:
#
# codex exec --cwd "$RESULT_DIR" "$(cat "$PROMPT")"
# claude -p "$(cat "$PROMPT")"
#
exit 64
