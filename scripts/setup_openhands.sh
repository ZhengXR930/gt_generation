#!/usr/bin/env bash
set -euo pipefail

# Reproduce the OpenHands checkout used by the PoC-generation experiments.
# The upstream source is deliberately not vendored into this repository.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${OPENHANDS_REPO:-${ROOT_DIR}/external/OpenHands}"
OPENHANDS_TAG="0.33.0"
OPENHANDS_COMMIT="35b381f3a8f4b5229934515e9f6b479d6d6415ef"
INSTALL=1
POETRY_BIN="${POETRY_BIN:-$(command -v poetry || true)}"
if [[ -z "${POETRY_BIN}" && -x "${HOME}/.local/pythons/cpython-3.11/bin/poetry" ]]; then
  POETRY_BIN="${HOME}/.local/pythons/cpython-3.11/bin/poetry"
fi

if [[ "${1:-}" == "--checkout-only" ]]; then
  INSTALL=0
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--checkout-only]" >&2
  exit 2
fi

if [[ -d "${TARGET}" && ! -f "${TARGET}/pyproject.toml" ]]; then
  # Older revisions tracked two OpenHands overlay files in this directory.
  # After updating, only ignored Python bytecode directories may remain. They
  # are safe to discard; any real file still causes a conservative refusal.
  find "${TARGET}" -type f -name '*.pyc' -delete
  find "${TARGET}" -depth -type d -empty -delete
fi

if [[ -e "${TARGET}" && ! -f "${TARGET}/pyproject.toml" ]]; then
  echo "Refusing to overwrite incomplete path: ${TARGET}" >&2
  echo "Move that path aside, then run this script again." >&2
  exit 2
fi

if [[ ! -f "${TARGET}/pyproject.toml" ]]; then
  mkdir -p "$(dirname "${TARGET}")"
  git clone --depth 1 --branch "${OPENHANDS_TAG}" \
    https://github.com/All-Hands-AI/OpenHands.git "${TARGET}"
fi

ACTUAL_COMMIT="$(git -C "${TARGET}" rev-parse HEAD)"
if [[ "${ACTUAL_COMMIT}" != "${OPENHANDS_COMMIT}" ]]; then
  echo "Unexpected OpenHands revision at ${TARGET}." >&2
  echo "Expected ${OPENHANDS_COMMIT}, got ${ACTUAL_COMMIT}." >&2
  exit 2
fi

if [[ "${INSTALL}" == "1" ]]; then
  if [[ -z "${POETRY_BIN}" ]]; then
    echo "Poetry is required to install OpenHands dependencies." >&2
    echo "Install Poetry, set POETRY_BIN, or rerun with --checkout-only and provide OPENHANDS_PYTHON." >&2
    exit 2
  fi
  (
    cd "${TARGET}"
    "${POETRY_BIN}" install --no-interaction
    OPENHANDS_PYTHON="$("${POETRY_BIN}" env info --executable)"
    "${OPENHANDS_PYTHON}" -m pip install \
      tomli-w==1.2.0 \
      simple-parsing==0.1.9 \
      sqlalchemy==2.0.51 \
      -e "${ROOT_DIR}/external/cybergym"
  )
fi

echo "OpenHands ${OPENHANDS_TAG} is ready at ${TARGET}"
