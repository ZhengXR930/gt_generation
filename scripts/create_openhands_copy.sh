#!/usr/bin/env bash
set -euo pipefail

# Create an editable OpenHands checkout without modifying the pinned pristine
# checkout used by PoC-generation evaluation.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${OPENHANDS_BASE_REPO:-${ROOT_DIR}/external/OpenHands}"
TARGET_ROOT="${OPENHANDS_COPY_ROOT:-${ROOT_DIR}/external/OpenHands-experiments}"
TARGET=""
INSTALL=0
OPENHANDS_COMMIT="35b381f3a8f4b5229934515e9f6b479d6d6415ef"

usage() {
  cat >&2 <<EOF
Usage: $0 NAME [--source PATH] [--target PATH] [--install]

Create an editable OpenHands copy from the pinned pristine checkout.

Defaults:
  source: ${ROOT_DIR}/external/OpenHands
  target: ${ROOT_DIR}/external/OpenHands-experiments/NAME

Use the editable copy only from an independent experiment or reward-framework
launcher that accepts:
  --openhands-repo <target>

Baseline/remote PoC evaluation should keep using the pristine source.
EOF
}

NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      SOURCE="$2"
      shift 2
      ;;
    --target)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      TARGET="$2"
      shift 2
      ;;
    --install)
      INSTALL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
    *)
      if [[ -n "${NAME}" ]]; then
        echo "Only one NAME is allowed." >&2
        usage
        exit 2
      fi
      NAME="$1"
      shift
      ;;
  esac
done

if [[ -z "${NAME}" ]]; then
  usage
  exit 2
fi
if [[ ! "${NAME}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "NAME may contain only letters, digits, dot, underscore, and dash." >&2
  exit 2
fi

SOURCE="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "${SOURCE}")"
if [[ -z "${TARGET}" ]]; then
  TARGET="${TARGET_ROOT}/${NAME}"
fi
TARGET="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "${TARGET}")"

if [[ ! -f "${SOURCE}/pyproject.toml" ]]; then
  echo "OpenHands source is missing or incomplete: ${SOURCE}" >&2
  echo "Run scripts/setup_openhands.sh first." >&2
  exit 2
fi

ACTUAL_COMMIT="$(git -C "${SOURCE}" rev-parse HEAD)"
if [[ "${ACTUAL_COMMIT}" != "${OPENHANDS_COMMIT}" ]]; then
  echo "Refusing to copy a non-pristine OpenHands revision." >&2
  echo "Expected ${OPENHANDS_COMMIT}, got ${ACTUAL_COMMIT} at ${SOURCE}." >&2
  exit 2
fi

dirty_lines=()
while IFS= read -r line; do
  [[ -z "${line}" || "${line}" == "?? uv.lock" ]] && continue
  dirty_lines+=("${line}")
done < <(git -C "${SOURCE}" status --porcelain)
if [[ "${#dirty_lines[@]}" -gt 0 ]]; then
  echo "Refusing to copy from a modified OpenHands checkout:" >&2
  printf '  %s\n' "${dirty_lines[@]}" >&2
  exit 2
fi

if [[ -e "${TARGET}" ]]; then
  echo "Refusing to overwrite existing target: ${TARGET}" >&2
  exit 2
fi

mkdir -p "$(dirname "${TARGET}")"
git clone --no-hardlinks "${SOURCE}" "${TARGET}"
git -C "${TARGET}" checkout -B "openhands-edit-${NAME}" "${OPENHANDS_COMMIT}"

if [[ "${INSTALL}" == "1" ]]; then
  OPENHANDS_REPO="${TARGET}" "${ROOT_DIR}/scripts/setup_openhands.sh"
fi

cat <<EOF
Created editable OpenHands copy:
  ${TARGET}

Pristine baseline remains:
  ${SOURCE}

Use the editable copy explicitly from an independent experiment or
reward-framework launcher:
  --openhands-repo ${TARGET}

Keep baseline/remote evaluation on the pristine checkout.
EOF
