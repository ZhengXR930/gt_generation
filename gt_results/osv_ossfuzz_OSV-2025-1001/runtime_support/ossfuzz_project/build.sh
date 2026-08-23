#!/bin/bash -eu
# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################

export GEM_HOME=$OUT/ox-gem
export GEM_PATH=$OUT/ox-gem

cd $SRC/ox-ruby
gem build

# Build the native ox extension with ASan instrumentation.  The original
# OSS-Fuzz wrapper depends on the base-builder-ruby image's preinstalled Ruzzy
# tree; the local runtime image does not have that tree, so use a direct
# one-input Ruby validator instead.
gem_cflags="${CFLAGS//-gline-tables-only/-g}"

gem install --local --ignore-dependencies --install-dir "$GEM_HOME" --no-document *.gem -- \
  --with-cflags="${gem_cflags}" \
  --with-ldflags="-fsanitize=address"

cp /gt/runtime_support/ox_repro.rb "$OUT/ox_repro.rb"

cat > "$OUT/fuzz_parse" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

this_dir=$(cd "$(dirname "$0")" && pwd)
export GEM_HOME="$this_dir/ox-gem"
export GEM_PATH="$this_dir/ox-gem"

asan_runtime=$(find /usr/lib/x86_64-linux-gnu -maxdepth 1 -name 'libasan.so.*' 2>/dev/null | sort | tail -n 1)
if [ -n "${asan_runtime}" ]; then
  export LD_PRELOAD="${asan_runtime}${LD_PRELOAD:+:${LD_PRELOAD}}"
fi

export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1}"
ruby "$this_dir/ox_repro.rb" "$@"
SH

chmod +x "$OUT/fuzz_parse"
