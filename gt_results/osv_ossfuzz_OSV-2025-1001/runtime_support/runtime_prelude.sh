#!/usr/bin/env bash
set -euo pipefail

cd "${SRC:?}"

# The upstream OSS-Fuzz builder checks out this repository under $SRC/ox-ruby
# and copies Ruby harnesses under $SRC/harnesses.
ln -sfn . ox-ruby
mkdir -p harnesses
cp /gt/runtime_support/ossfuzz_project/fuzz_parse.rb harnesses/fuzz_parse.rb
