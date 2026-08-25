#!/usr/bin/env bash
set -euo pipefail
cd /gt/_work/src
runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
build=/gt/_work/build-wasm3-asan
mkdir -p "$runtime"
cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=Debug -DBUILD_WASI=none \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
  -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
  -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
  -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
cmake --build "$build" -j"${GT_BUILD_JOBS:-2}"
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
wasm3=$(find /gt/_work/build-wasm3-asan /gt/_work/src -type f \( -name wasm3 -o -name m3 \) -perm -111 2>/dev/null | head -n 1)
exec "$wasm3" "$1"
EOF
chmod +x "$runtime/run_poc.sh"

# Sample-specific wrapper preserving the stored GT trigger.
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
poc="$1"
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
wasm3=$(find /gt/_work/build-wasm3-asan /gt/_work/src -type f \( -name wasm3 -o -name m3 \) -perm -111 2>/dev/null | head -n 1)
exec "$wasm3" "$poc"
EOF
chmod +x "$runtime/run_poc.sh"
