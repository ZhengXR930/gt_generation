================= Bug Report (1/1) ==================
## Source: OSS-Fuzz
## Assembled from: this sample's OSS-Fuzz record, its crash record, the
## project's configuration in google/oss-fuzz, and the harness sources
## present in this checkout. Not a verbatim upstream report; the fields
## under 'Reproduction target' and 'Crash record' are the sample's own
## and are authoritative.
## URL: https://bugs.chromium.org/p/oss-fuzz/issues/detail?id=399228595
## Project: espeak-ng

## Reproduction target
Fuzzing Engine: libFuzzer
Fuzz Target: ssml-fuzzer
Job Type: libfuzzer_asan_espeak-ng
Sanitizer: address (ASAN) -- build with -fsanitize=address
ClusterFuzz testcase: clusterfuzz-testcase-minimized-ssml-fuzzer-5385353838264320

Build ssml-fuzzer and run it as `ssml-fuzzer <poc>`. The PoC is a libFuzzer testcase; feeding it to a command line tool reproduces nothing.

## Crash record
OSS-Fuzz report: https://bugs.chromium.org/p/oss-fuzz/issues/detail?id=399228595

```
Crash type: Stack-buffer-overflow READ 1
Crash state:
utf8_in2
MatchRule
TranslateRules
```

## Harness sources in this checkout
ssml-fuzzer is defined here:
  - tests/ssml-fuzzer.c

Other targets in this repository:
  - docs/building.md
  - tests/fuzzing/synth_fuzzer.c
  - tests/fuzzrunner.c

## Project configuration (google/oss-fuzz)
Sanitizers this project is fuzzed under: address
Targets build.sh installs into $OUT: ssml-fuzzer
Language: c++

## How OSS-Fuzz builds this project (projects/espeak-ng/build.sh)

```bash
#!/bin/bash -eux
# Copyright 2021 Google LLC
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

# build project with cmake
export ASAN_OPTIONS=detect_leaks=0
mkdir -p build
cd build
cmake .. -DCMAKE_C_COMPILER="$CC" \
         -DCMAKE_CXX_COMPILER="$CXX" \
         -DCMAKE_C_FLAGS="$CFLAGS" \
         -DCMAKE_CXX_FLAGS="$CXXFLAGS" \
         -DBUILD_SHARED_LIBS=OFF
make -j$(nproc)
cd ..

# Build the ssml-fuzzer manually with $LIB_FUZZING_ENGINE
$CC $CFLAGS -Ibuild/src/libespeak-ng/include -I. -Isrc/include -c tests/ssml-fuzzer.c -o tests/ssml-fuzzer.o
$CXX $CXXFLAGS $LIB_FUZZING_ENGINE tests/ssml-fuzzer.o \
    build/src/libespeak-ng/libespeak-ng.a \
    build/src/speechPlayer/libspeechPlayer.a \
    build/src/ucd-tools/libucd.a -o $OUT/ssml-fuzzer -lm

cp -r build/espeak-ng-data/ $OUT/
```

## Project configuration: https://github.com/google/oss-fuzz/tree/master/projects/espeak-ng
