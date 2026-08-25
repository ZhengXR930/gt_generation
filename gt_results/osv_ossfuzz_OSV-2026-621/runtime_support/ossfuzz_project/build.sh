#!/bin/bash -eu
set -o pipefail
export CC=${CC:-clang}
export CXX=${CXX:-clang++}
export CFLAGS="${CFLAGS:-}"
export CXXFLAGS="${CXXFLAGS:-$CFLAGS}"
export LDFLAGS="${LDFLAGS:-}"
rm -rf /tmp/libical-install /tmp/libical-build
mkdir -p /tmp/libical-install /tmp/libical-build "$OUT"
cmake -S . -B /tmp/libical-build \
  -DCMAKE_INSTALL_PREFIX=/tmp/libical-install \
  -DLIBICAL_STATIC=ON \
  -DLIBICAL_GLIB=False \
  -DLIBICAL_GLIB_BUILD_DOCS=False \
  -DLIBICAL_GOBJECT_INTROSPECTION=False \
  -DLIBICAL_JAVA_BINDINGS=False \
  -DLIBICAL_BUILD_TESTING=True \
  -DCMAKE_C_COMPILER="$CC" \
  -DCMAKE_CXX_COMPILER="$CXX" \
  -DCMAKE_C_FLAGS="$CFLAGS" \
  -DCMAKE_CXX_FLAGS="$CXXFLAGS" \
  -DCMAKE_EXE_LINKER_FLAGS="$LDFLAGS" \
  -DCMAKE_SHARED_LINKER_FLAGS="$LDFLAGS"
cmake --build /tmp/libical-build -j"${GT_BUILD_JOBS:-$(nproc)}"
cmake --install /tmp/libical-build
ICU_LIBS="$(pkg-config --libs icu-i18n icu-uc icu-io)"
INC="-I/tmp/libical-install/include -I$SRC/libical/src/libicalvcard -I$SRC/libical/src/libical -I/gt/_work/src/src/libicalvcard -I/gt/_work/src/src/libical"
$CXX $CXXFLAGS $INC -std=c++11 "$SRC/libicalvcard_fuzzer.cc" \
  /tmp/libical-install/lib/libicalvcard.a /tmp/libical-install/lib/libical.a \
  -o "$OUT/libicalvcard_fuzzer" $LDFLAGS -fsanitize=fuzzer $ICU_LIBS
cp "$OUT/libicalvcard_fuzzer" /gt/libicalvcard_fuzzer
