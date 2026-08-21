#!/usr/bin/env bash
set -euo pipefail

ASSET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE=gt-memory-env:latest
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
if [[ $# -eq 0 ]]; then
  echo "usage: $0 <build-or-reproduction command>" >&2
  exit 2
fi
PROXY_ENV=()
for _v in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
  if [[ -n "${!_v:-}" ]]; then PROXY_ENV+=(-e "${_v}=${!_v}"); fi
done
exec docker run --rm \
  -e HOME=/tmp \
  -e HOST_UID="${HOST_UID}" \
  -e HOST_GID="${HOST_GID}" \
  "${PROXY_ENV[@]}" \
  -v "${ASSET_DIR}:/gt" \
  -v /data00/home/zhengxinran/Documents/trae_projects/test/gt_generation:/repo:ro \
  -w /gt/_work/src \
  "${IMAGE}" \
  bash -lc 'trap '"'"'chown -R "${HOST_UID}:${HOST_GID}" /gt >/dev/null 2>&1 || true'"'"' EXIT; git config --global --add safe.directory /gt/_work/src >/dev/null 2>&1 || true; '"$*"
