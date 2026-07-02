#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 TRAJECTORY OUT_DIR [extra external_interpreter.cli args...]" >&2
  exit 2
fi

trajectory="$1"
out_dir="$2"
shift 2

PYTHONPATH="${PYTHONPATH:-.}" python -m external_interpreter.cli \
  --trajectory "$trajectory" \
  --out-dir "$out_dir" \
  "$@"
