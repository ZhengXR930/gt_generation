#!/usr/bin/env bash
# Start the local CyberGym validation server used to judge submitted PoCs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$ROOT/server/logs"

exec python3 -m cybergym.server \
  --host 0.0.0.0 \
  --port 8666 \
  --db_path "$ROOT/server/poc.db" \
  --log_dir "$ROOT/server/logs"
