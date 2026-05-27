#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/materialize_final_dataset.py --output final_dataset --pull-arvo --resume
