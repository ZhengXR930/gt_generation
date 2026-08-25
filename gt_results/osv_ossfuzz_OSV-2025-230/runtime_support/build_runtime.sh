#!/usr/bin/env bash
set -euo pipefail
cd /gt/_work/src
runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
build=/gt/_work/build-wamr-fast-asan
mkdir -p "$runtime"
cmake -S tests/fuzz/wasm-mutator-fuzz -B "$build" -G Ninja \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
  -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
  -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
  -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address" \
  -DWAMR_BUILD_AOT=0 -DWAMR_BUILD_JIT=0 -DWAMR_BUILD_FAST_INTERP=1
cmake --build "$build" --target wasm_mutator_fuzz -j"${GT_BUILD_JOBS:-2}"
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
bin=$(find /gt/_work/build-wamr-fast-asan /gt/_work/src -type f -name wasm_mutator_fuzz -perm -111 2>/dev/null | head -n 1)
exec "$bin" -runs=1 "$1"
EOF
chmod +x "$runtime/run_poc.sh"
