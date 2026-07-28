#!/usr/bin/env bash
set -euo pipefail

ASSET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker run --rm --platform linux/amd64 -v "${ASSET_DIR}/poc:/tmp/poc:ro" --entrypoint /bin/bash n132/arvo:23153-vul -c '/bin/arvo run'
