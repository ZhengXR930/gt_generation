#!/usr/bin/env bash
set -euo pipefail
cd /gt/_work/src
if command -v apt-get >/dev/null 2>&1 && ! command -v re2c >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y re2c
fi
runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
mkdir -p "$runtime"
if [ ! -x sapi/cli/php ]; then
  ./buildconf --force || true
  CC=clang CXX=clang++ CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
    CXXFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
    LDFLAGS="-fsanitize=address" \
    ./configure --disable-all --enable-cli --enable-debug --enable-address-sanitizer
  make -j"${GT_BUILD_JOBS:-2}" sapi/cli/php
fi
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
exec /gt/_work/src/sapi/cli/php "$1"
EOF
chmod +x "$runtime/run_poc.sh"

# Sample-specific wrapper preserving the stored GT trigger.
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
poc="$1"
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
exec /gt/_work/src/sapi/cli/php "$poc"
EOF
chmod +x "$runtime/run_poc.sh"
