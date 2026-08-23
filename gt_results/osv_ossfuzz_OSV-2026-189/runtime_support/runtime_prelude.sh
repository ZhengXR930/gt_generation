#!/usr/bin/env bash
set -euo pipefail

cd "${SRC:?}"

# GPSD's current OSS-Fuzz recipe lives in adalogics/ada-fuzzers.  The runtime
# source is restored directly at $SRC, while the recipe expects $SRC/gpsd.
ln -sfn . gpsd

support_root=/gt/runtime_support/ossfuzz_project
deps_root="${WORK:-/gt/_work/work}/gpsd_runtime_deps"
ada_repo="${deps_root}/ada-fuzzers"
mkdir -p "${deps_root}"

if [ ! -d "${ada_repo}/.git" ]; then
  git clone --depth 1 https://github.com/adalogics/ada-fuzzers "${ada_repo}"
fi

cp "${ada_repo}/projects/gpsd/build.sh" "${support_root}/build.sh"
cp -a "${ada_repo}/projects/gpsd/fuzzer" "${SRC}/gpsd/"
cp -a "${ada_repo}/projects/gpsd/corp" "${SRC}/gpsd/"
