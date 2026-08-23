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
cd "${GT_OSS_FUZZ_WORKDIR:-/gt/_work/quickjs}"

#!/bin/bash -eu
# Copyright 2020 Google Inc.
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

# build quickjs
# Makefile should not override CFLAGS
sed -i -e 's/CFLAGS=/CFLAGS+=/' Makefile
sed -i -e 's/#define USE_WORKER/\/\/#define USE_WORKER/' quickjs-libc.c
CONFIG_CLANG=y make libquickjs.fuzz.a .obj/fuzz_common.o .obj/libregexp.fuzz.o .obj/cutils.fuzz.o .obj/libunicode.fuzz.o
zip -r $OUT/fuzz_eval_seed_corpus.zip $SRC/quickjs-corpus/js/*.js
zip -r $OUT/fuzz_compile_seed_corpus.zip $SRC/quickjs-corpus/js/*.js

build_fuzz_target () {
    local target=$1
    shift
    $CC $CFLAGS -I. -c fuzz/$target.c -o $target.o
    $CXX $CXXFLAGS $target.o -o $OUT/$target $@ $LIB_FUZZING_ENGINE
}

build_fuzz_target fuzz_eval .obj/fuzz_common.o libquickjs.fuzz.a
build_fuzz_target fuzz_compile .obj/fuzz_common.o libquickjs.fuzz.a
build_fuzz_target fuzz_regexp .obj/libregexp.fuzz.o .obj/cutils.fuzz.o .obj/libunicode.fuzz.o

cp fuzz/fuzz.dict $OUT/fuzz_eval.dict
cp fuzz/fuzz.dict $OUT/fuzz_compile.dict
