#!/usr/bin/env bash
set -euo pipefail

ASSET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE=gt-memory-env:latest
REPO_ROOT="${GT_REPO_ROOT:-}"
if [[ -z "${REPO_ROOT}" ]]; then
  if git -C "${ASSET_DIR}" rev-parse --show-toplevel >/dev/null 2>&1; then
    REPO_ROOT="$(git -C "${ASSET_DIR}" rev-parse --show-toplevel)"
  else
    REPO_ROOT="$(cd "${ASSET_DIR}/../.." && pwd)"
  fi
fi
if [[ ! -d "${REPO_ROOT}/gt_generation" ]]; then
  echo "cannot locate gt_generation repo root; set GT_REPO_ROOT" >&2
  exit 2
fi
if [[ $# -eq 0 ]]; then
  echo "usage: $0 <build-or-reproduction command>" >&2
  exit 2
fi
PROXY_ENV=()
for _v in http_proxy https_proxy no_proxy all_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY GT_APT_MIRROR GT_DEBIAN_APT_MIRROR GT_DEBIAN_SECURITY_MIRROR GT_APK_MIRROR GT_PIP_INDEX_URL GT_PIP_TRUSTED_HOST GT_NPM_REGISTRY GT_GOPROXY GT_GOSUMDB GT_RUSTUP_DIST_SERVER GT_RUSTUP_UPDATE_ROOT GT_CARGO_REGISTRY_INDEX GT_NETWORK_BOOTSTRAP_DISABLED; do
  if [[ -n "${!_v:-}" ]]; then PROXY_ENV+=(-e "${_v}"); fi
done
USER_ENV=(--user "$(id -u):$(id -g)")
if [[ "${GT_BUILD_AS_ROOT:-0}" == "1" ]]; then USER_ENV=(); fi
exec docker run --rm "${USER_ENV[@]}" -e HOME=/tmp "${PROXY_ENV[@]}" -v "${ASSET_DIR}:/gt" -v "${REPO_ROOT}:/repo:ro" -w /gt/_work/src "${IMAGE}" bash -lc 'if [[ -f /usr/local/bin/gt-network-bootstrap ]]; then
  source /usr/local/bin/gt-network-bootstrap
elif [[ -f /repo/docker/gt-memory-env/network_bootstrap.sh ]]; then
  source /repo/docker/gt-memory-env/network_bootstrap.sh
fi

exec bash -lc "$*"
' build-wrapper "$*"
