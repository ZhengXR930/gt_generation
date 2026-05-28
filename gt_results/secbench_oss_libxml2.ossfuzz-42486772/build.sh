#!/usr/bin/env bash
set -euo pipefail

# Rebuild helper for secbench_oss_libxml2.ossfuzz-42486772.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$ROOT/work/secbench_oss_libxml2.ossfuzz-42486772"
SRC="$WORK/src"
POC="$WORK/tmp/poc"
REPO="https://github.com/GNOME/libxml2"
VULN_COMMIT="87d20b554c6a90e7ece1cc7391c005089bf85b78"
POC_URL="https://oss-fuzz.com/download?testcase_id=6544709487689728"

install_project_deps() {
  local deps=(
    # libxml2 builds with the baseline gt-memory-env toolchain.
  )
  if [ "${#deps[@]}" -gt 0 ]; then
    apt-get update
    apt-get install -y --no-install-recommends "${deps[@]}"
    rm -rf /var/lib/apt/lists/*
  fi
}

install_project_deps

mkdir -p "$WORK/tmp"
curl -L -o "$POC" "$POC_URL"

if [ ! -d "$SRC/.git" ]; then
  rm -rf "$SRC"
  git clone "$REPO" "$SRC"
fi
cd "$SRC"
git fetch --all --tags --prune
git checkout "$VULN_COMMIT"

build_one() {
  local kind="$1"
  local cflags="$2"
  local ldflags="$3"
  local build_dir="$WORK/build_${kind}"
  rm -rf "$build_dir"
  cp -a "$SRC" "$build_dir"
  cd "$build_dir"
  if [ -x ./autogen.sh ]; then
    CFLAGS="$cflags" LDFLAGS="$ldflags" ./autogen.sh --without-python
  else
    CFLAGS="$cflags" LDFLAGS="$ldflags" ./configure --without-python
  fi
  make -j"${JOBS:-2}"
}

build_one sanitizer "-g -O1 -fno-omit-frame-pointer -fsanitize=address" "-fsanitize=address"
build_one valgrind "-g -O0 -fno-omit-frame-pointer" ""

echo "Sanitizer binary: $WORK/build_sanitizer/xmllint"
echo "Valgrind binary: $WORK/build_valgrind/xmllint"
echo "PoC: $POC"
echo "Run sanitizer: $WORK/build_sanitizer/xmllint --stream --xinclude $POC"
echo "Run valgrind: valgrind --track-origins=yes --leak-check=full $WORK/build_valgrind/xmllint --stream --xinclude $POC"
