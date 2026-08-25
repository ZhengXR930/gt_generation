#!/usr/bin/env bash
set -euo pipefail
cd /gt/_work/src
runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
build=/gt/_work/build-asan-internal
mkdir -p "$runtime"
cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=RelWithDebInfo   -DBUILD_TESTING=OFF -DOPENEXR_BUILD_TOOLS=ON -DOPENEXR_FORCE_INTERNAL_DEFLATE=ON   -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++   -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer"   -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer"   -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
cmake --build "$build" --target exrcheck -j"${GT_BUILD_JOBS:-2}"
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
poc="$1"
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
exrcheck=$(find /gt/_work/build-asan-internal /gt/_work/src -type f -name exrcheck -perm -111 2>/dev/null | head -n 1)
"$exrcheck" -c "$poc" || "$exrcheck" -s -c "$poc" || exec "$exrcheck" "$poc"
EOF
chmod +x "$runtime/run_poc.sh"
