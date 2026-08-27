#!/usr/bin/env bash
set -euo pipefail
cd /gt/_work/src
runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
mkdir -p "$runtime"
git config --global --add safe.directory /gt/_work/src 2>/dev/null || true
git config --global --add safe.directory "*" 2>/dev/null || true
for attempt in 1 2 3; do
  git submodule sync --recursive || true
  if git submodule update --init --recursive; then break; fi
  if [[ "$attempt" == 3 ]]; then exit 1; fi
  sleep 5
done
make -j"${GT_BUILD_JOBS:-2}" build=sanitize HAVE_X11=no HAVE_GLUT=no HAVE_CURL=no \
  USE_SYSTEM_LIBS=no USE_SYSTEM_FREETYPE=no USE_SYSTEM_HARFBUZZ=no USE_SYSTEM_JBIG2DEC=no \
  USE_SYSTEM_LCMS2=no USE_SYSTEM_LIBJPEG=no USE_SYSTEM_OPENJPEG=no USE_SYSTEM_ZLIB=no \
  XCFLAGS="-DFZ_ENABLE_ICC=0 -Ithirdparty/libjpeg"
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
mutool=$(find /gt/_work/src/build -type f -name mutool -perm -111 2>/dev/null | head -n 1)
exec "$mutool" draw -o /tmp/mupdf-out-%d.png "$1"
EOF
chmod +x "$runtime/run_poc.sh"

# Sample-specific wrapper preserving the stored GT trigger.
cat > "$runtime/run_poc.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
poc="$1"
export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
mutool=$(find /gt/_work/src/build -type f -name mutool -perm -111 2>/dev/null | head -n 1)
exec "$mutool" draw -o /tmp/mupdf-out-%d.png "$poc"
EOF
chmod +x "$runtime/run_poc.sh"
