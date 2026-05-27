#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker build -t gt-memory-env:latest -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"
