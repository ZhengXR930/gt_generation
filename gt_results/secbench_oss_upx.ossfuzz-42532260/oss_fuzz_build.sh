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
cd "${GT_OSS_FUZZ_WORKDIR:-/gt/_work/upx}"

# Staged from official google/oss-fuzz projects/<project>/build.sh
#!/bin/bash -eu
# Copyright 2023 Google LLC
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

# Temporary fix for clang bug of upx
sed -i 's/ \&\& __clang_major__ < 15//m' /src/upx/src/util/util.cpp
git apply $SRC/upx/fuzzers/build.patch

# build project
# e.g.
mkdir -p build/debug
cd build/debug
cmake ../..

for fuzzer in $(find $SRC -name '*_fuzzer.cpp'); do
    fuzz_basename=$(basename -s .cpp $fuzzer)
    cmake --build . --target $fuzz_basename -v
done

cp ./*_fuzzer $OUT/

mkdir -p $SRC/corpus/

cp $SRC/testsuite3/files/corpus/* $SRC/corpus/

for architecture in $(ls $SRC/testsuite/files/packed/); do
    for file in $(ls $SRC/testsuite/files/packed/$architecture); do
        cp $SRC/testsuite/files/packed/$architecture/$file $SRC/corpus/$architecture-$file
    done
done

for architecture in $(ls $SRC/testsuite2/files/all/); do
    for file in $(ls $SRC/testsuite2/files/all/$architecture); do
        cp $SRC/testsuite2/files/all/$architecture/$file $SRC/corpus/$architecture-$file
    done
done

# build corpora
# e.g.
for fuzzer in $(find $SRC -name '*_fuzzer.cpp'); do
    fuzz_basename=$(basename -s .cpp $fuzzer)
    zip -q $OUT/${fuzz_basename}_seed_corpus.zip $SRC/corpus/*
done
