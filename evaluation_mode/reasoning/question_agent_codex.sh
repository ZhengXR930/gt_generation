#!/usr/bin/env bash
set -euo pipefail

ROLE_FILE=""
INPUT=""
OUTPUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role-file) ROLE_FILE="$2"; shift 2 ;;
    --input) INPUT="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$ROLE_FILE" || -z "$INPUT" || -z "$OUTPUT" ]]; then
  echo "usage: $0 --role-file ROLE --input REQUEST --output RESPONSE" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEMA="$(dirname "${BASH_SOURCE[0]}")/question_output.schema.json"
PROMPT="$(<"$ROLE_FILE")

Input JSON:
$(<"$INPUT")

Return only the requested JSON object. Do not use tools or inspect any files."

exec codex exec \
  --cd "$ROOT" \
  --sandbox read-only \
  --ephemeral \
  --output-schema "$SCHEMA" \
  --output-last-message "$OUTPUT" \
  "$PROMPT"
