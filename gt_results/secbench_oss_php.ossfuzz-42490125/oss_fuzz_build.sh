#!/usr/bin/env bash
set -euo pipefail
export SRC="${SRC:-/gt/_work}"
export OUT="${OUT:-/gt/_out}"
export WORK="${WORK:-/gt/_work}"
export CC="${CC:-clang}"
export CXX="${CXX:-clang++}"
export CFLAGS="${CFLAGS:--O1 -fno-omit-frame-pointer -gline-tables-only -fsanitize=address}"
export CXXFLAGS="${CXXFLAGS:--O1 -fno-omit-frame-pointer -gline-tables-only -fsanitize=address}"
export SANITIZER="${SANITIZER:-address}"
export FUZZING_ENGINE="${FUZZING_ENGINE:-libfuzzer}"
export ARCHITECTURE="${ARCHITECTURE:-x86_64}"
export LIB_FUZZING_ENGINE="${LIB_FUZZING_ENGINE:--fsanitize=fuzzer}"
export FUZZER_LIB="${FUZZER_LIB:-$LIB_FUZZING_ENGINE}"
if [[ ! -e /usr/lib/libFuzzingEngine.a ]]; then
  _gt_fuzzer_lib="$(find /usr/lib /usr/local/lib -path "*/lib/clang/*/lib/linux/libclang_rt.fuzzer_no_main-x86_64.a" -print -quit 2>/dev/null || true)"
  if [[ -n "$_gt_fuzzer_lib" ]]; then ln -sf "$_gt_fuzzer_lib" /usr/lib/libFuzzingEngine.a 2>/dev/null || true; fi
  unset _gt_fuzzer_lib
fi
if ! ldconfig -p 2>/dev/null | grep -q "libc++\.so" && [[ ! -e /usr/lib/x86_64-linux-gnu/libc++.so ]]; then
  _gt_stdlib="$(ldconfig -p 2>/dev/null | awk '/libstdc\+\+\.so/{print $NF; exit}')"
  if [[ -n "$_gt_stdlib" ]]; then ln -sf "$_gt_stdlib" /usr/lib/x86_64-linux-gnu/libc++.so 2>/dev/null || true; fi
  unset _gt_stdlib
fi
git config --global --add safe.directory /gt/_work/src 2>/dev/null || true
cd "${GT_OSS_FUZZ_WORKDIR:-/gt/_work/php-src}"

# Staged from official google/oss-fuzz projects/<project>/build.sh
#!/bin/bash -eu
# Copyright 2019 Google Inc.
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

# build oniguruma and link statically
pushd oniguruma
autoreconf -vfi
./configure
make -j$(nproc)
popd
export ONIG_CFLAGS="-I$PWD/oniguruma/src"
export ONIG_LIBS="-L$PWD/oniguruma/src/.libs -l:libonig.a"

# PHP's zend_function union is incompatible with the object-size sanitizer
export CFLAGS="$CFLAGS -fno-sanitize=object-size"
export CXXFLAGS="$CXXFLAGS -fno-sanitize=object-size"

# build project
./buildconf
./configure \
    --disable-all \
    --enable-debug-assertions \
    --enable-option-checking=fatal \
    --enable-fuzzer \
    --enable-exif \
    --enable-mbstring \
    --without-pcre-jit \
    --disable-phpdbg \
    --disable-cgi \
    --with-pic
make -j$(nproc)

# Generate corpuses and dictionaries.
sapi/cli/php sapi/fuzzer/generate_all.php

# Copy dictionaries to expected locations.
cp sapi/fuzzer/dict/unserialize $OUT/php-fuzz-unserialize.dict
cp sapi/fuzzer/dict/parser $OUT/php-fuzz-parser.dict
cp sapi/fuzzer/json.dict $OUT/php-fuzz-json.dict

FUZZERS="php-fuzz-json
php-fuzz-exif
php-fuzz-mbstring
php-fuzz-unserialize
php-fuzz-unserializehash
php-fuzz-parser
php-fuzz-execute"
for fuzzerName in $FUZZERS; do
	cp sapi/fuzzer/$fuzzerName $OUT/
done
# copy corpora from source
for fuzzerName in `ls sapi/fuzzer/corpus`; do
	zip -j $OUT/php-fuzz-${fuzzerName}_seed_corpus.zip sapi/fuzzer/corpus/${fuzzerName}/*
done
