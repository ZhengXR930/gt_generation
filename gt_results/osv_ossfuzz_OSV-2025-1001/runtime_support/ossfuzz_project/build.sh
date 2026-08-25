#!/bin/bash -eu
# Copyright 2024 Google LLC
export GEM_HOME=$OUT/fuzz_parse-gem
BUILD=$WORK/Build
cd $SRC/ox-ruby
gem build
export LDFLAGS="-fsanitize=address ${LDFLAGS:-}"
RUZZY_DEBUG=1 gem install --development --verbose *.gem || gem install --verbose *.gem
cp $SRC/harnesses/fuzz_parse.rb $OUT/ || true
cat > $OUT/fuzz_parse <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
this_dir=$(dirname "$0")
export GEM_HOME=$this_dir/fuzz_parse-gem
export GEM_PATH=$this_dir/fuzz_parse-gem
asan_rt=$(clang -print-file-name=libclang_rt.asan-x86_64.so)
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}:use_sigaltstack=0"
exec env LD_PRELOAD="$asan_rt" ruby -e '
  Gem.use_paths(ENV["GEM_HOME"], [ENV["GEM_HOME"]])
  require "ox"
  poc = ARGV.reverse.find { |a| File.file?(a) }
  exit 2 unless poc
  data = File.binread(poc)
  exit 0 if data.bytesize < 100
  begin
    Ox.parse(data)
  rescue Ox::ParseError, Ox::SyntaxError, EncodingError
  end
' -- "$@"
EOF
chmod +x $OUT/fuzz_parse
