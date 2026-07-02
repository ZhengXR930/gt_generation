#!/usr/bin/env bash
set -euo pipefail

# Re-materialize CyberGym/ARVO sample arvo_12419.
# Project-specific dependency/build adaptation should be appended below when
# producing debug/Valgrind/watchpoint binaries.

ARVO_ID=12419
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${ROOT_DIR}/../../work/cybergym_arvo50/arvo_12419"
mkdir -p "$WORK_DIR"

docker pull "n132/arvo:${ARVO_ID}-vul"
docker create --name "gt_${ARVO_ID}_vul_$$" "n132/arvo:${ARVO_ID}-vul"
docker cp "gt_${ARVO_ID}_vul_$$:/src" "$WORK_DIR/vul/src"
docker cp "gt_${ARVO_ID}_vul_$$:/out" "$WORK_DIR/vul/out"
docker cp "gt_${ARVO_ID}_vul_$$:/tmp/poc" "$WORK_DIR/vul/poc"
docker rm -f "gt_${ARVO_ID}_vul_$$"
docker rmi "n132/arvo:${ARVO_ID}-vul"

docker pull "n132/arvo:${ARVO_ID}-fix"
docker create --name "gt_${ARVO_ID}_fix_$$" "n132/arvo:${ARVO_ID}-fix"
docker cp "gt_${ARVO_ID}_fix_$$:/src" "$WORK_DIR/fix/src"
docker cp "gt_${ARVO_ID}_fix_$$:/out" "$WORK_DIR/fix/out"
docker rm -f "gt_${ARVO_ID}_fix_$$"
docker rmi "n132/arvo:${ARVO_ID}-fix"
