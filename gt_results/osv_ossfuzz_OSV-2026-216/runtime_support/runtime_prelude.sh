#!/usr/bin/env bash
set -euo pipefail

cd "${SRC:?}"

# Upstream builder checks out the repo as $SRC/mongoose and copies the fuzzer
# support file there.  The runtime restores the repo directly at $SRC.
ln -sfn . mongoose
