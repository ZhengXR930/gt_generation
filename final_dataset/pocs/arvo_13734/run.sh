#!/usr/bin/env bash
set -euo pipefail
docker run --rm \
  -v "$PWD/poc:/tmp/poc:ro" \
  n132/arvo:13734-vul \
  arvo
