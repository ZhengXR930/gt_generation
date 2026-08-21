#!/usr/bin/env bash
set -euo pipefail

ASSET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE=gt-memory-env:latest
if [[ $# -eq 0 ]]; then
  echo "usage: $0 <build-or-reproduction command>" >&2
  exit 2
fi
PROXY_ENV=()
for _v in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
  if [[ -n "${!_v:-}" ]]; then PROXY_ENV+=(-e "${_v}=${!_v}"); fi
done
USER_ENV=(--user "$(id -u):$(id -g)")
if [[ "${GT_BUILD_AS_ROOT:-0}" == "1" ]]; then USER_ENV=(); fi
exec docker run --rm "${USER_ENV[@]}" -e HOME=/tmp "${PROXY_ENV[@]}" -v "${ASSET_DIR}:/gt" -v /data00/home/zhengxinran/Documents/trae_projects/test/gt_generation:/repo:ro -w /gt/_work/src "${IMAGE}" bash -lc "$*"
