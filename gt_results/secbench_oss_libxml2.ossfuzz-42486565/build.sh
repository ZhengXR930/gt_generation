#!/usr/bin/env bash
set -euo pipefail

ASSET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE=gt-memory-env:latest
if [[ $# -eq 0 ]]; then
  echo "usage: $0 <build-or-reproduction command>" >&2
  exit 2
fi
exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "${ASSET_DIR}:/gt" -w /gt/_work/src "${IMAGE}" bash -lc "$*"
