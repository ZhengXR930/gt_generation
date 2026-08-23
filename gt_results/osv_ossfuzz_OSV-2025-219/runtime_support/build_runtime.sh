#!/usr/bin/env bash
set -euo pipefail
cd /gt/_work/src
runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
build=/gt/_work/build-openjpeg-asan
mkdir -p "$runtime"
cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=Debug -DBUILD_CODEC=ON \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
  -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
  -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
  -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
cmake --build "$build" -j"${GT_BUILD_JOBS:-2}"
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
opj=$(find /gt/_work/build-openjpeg-asan -type f -name opj_decompress -perm -111 2>/dev/null | head -n 1)
exec "$opj" -i "$1" -o /tmp/openjpeg-out.pnm
EOF
chmod +x "$runtime/run_poc.sh"
