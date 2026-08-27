#!/usr/bin/env bash
set -euo pipefail
cd /gt/_work/src
runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
build=/gt/_work/build-asan-internal
mkdir -p "$runtime"
git config --global --add safe.directory /gt/_work/src 2>/dev/null || true
git config --global --add safe.directory "*" 2>/dev/null || true
if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y libimath-dev libdeflate-dev zlib1g-dev
fi
cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=RelWithDebInfo   -DBUILD_TESTING=OFF -DOPENEXR_BUILD_TOOLS=ON -DOPENEXR_FORCE_INTERNAL_DEFLATE=OFF   -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++   -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer"   -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer"   -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
cmake --build "$build" --target exrcheck -j"${GT_BUILD_JOBS:-2}"
mkdir -p "$runtime/lib"
cp -a /usr/lib/x86_64-linux-gnu/libImath*.so* "$runtime/lib" 2>/dev/null || true
cp -a /usr/lib/x86_64-linux-gnu/libdeflate*.so* "$runtime/lib" 2>/dev/null || true
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
poc="$1"
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
export LD_LIBRARY_PATH="/gt/_work/runtime/${GT_SAMPLE_ID:-secbench_oss_openexr.ossfuzz-42524709}/lib:/gt/_work/build-asan-internal/src/lib/OpenEXRUtil:/gt/_work/build-asan-internal/src/lib/OpenEXR:/gt/_work/build-asan-internal/src/lib/OpenEXRCore:/gt/_work/build-asan-internal/src/lib/IlmThread:/gt/_work/build-asan-internal/src/lib/Iex:${LD_LIBRARY_PATH:-}"
exrcheck=$(find /gt/_work/build-asan-internal /gt/_work/src -type f -name exrcheck -perm -111 2>/dev/null | head -n 1)
"$exrcheck" -c "$poc" || "$exrcheck" -s -c "$poc" || exec "$exrcheck" "$poc"
EOF
chmod +x "$runtime/run_poc.sh"
