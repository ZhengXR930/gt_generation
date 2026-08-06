================= Bug Report (1/1) ==================
## Source: OSS-Fuzz
## Assembled from: this sample's OSS-Fuzz record, its crash record, the
## project's configuration in google/oss-fuzz, and the harness sources
## present in this checkout. Not a verbatim upstream report; the fields
## under 'Reproduction target' and 'Crash record' are the sample's own
## and are authoritative.
## URL: https://bugs.chromium.org/p/oss-fuzz/issues/detail?id=386713389
## Project: libavif

## Reproduction target
Fuzzing Engine: libFuzzer
Fuzz Target: avif_fuzztest_properties@PropertiesAvifFuzzTest.PropsValid
Job Type: libfuzzer_asan_libavif
Sanitizer: address (ASAN) -- build with -fsanitize=address
ClusterFuzz testcase: clusterfuzz-testcase-minimized-avif_fuzztest_properties@PropertiesAvifFuzzTest.PropsValid-5539310685716480

Build avif_fuzztest_properties@PropertiesAvifFuzzTest.PropsValid and run it as `avif_fuzztest_properties@PropertiesAvifFuzzTest.PropsValid <poc>`. The PoC is a libFuzzer testcase; feeding it to a command line tool reproduces nothing.

## Crash record
OSS-Fuzz report: https://bugs.chromium.org/p/oss-fuzz/issues/detail?id=386713389

```
Crash type: Heap-buffer-overflow READ 16
Crash state:
avifImageAddUUIDProperty
avif::testutil::PropsValid
```

## Harness sources in this checkout
No file in this checkout defines avif_fuzztest_properties@PropertiesAvifFuzzTest.PropsValid. Its harness most likely lives in the google/oss-fuzz project directory (see the build.sh below), not in this repository.

Files here that define LLVMFuzzerTestOneInput:
  - tests/oss-fuzz/build.sh

## Project configuration (google/oss-fuzz)
Sanitizers this project is fuzzed under: address, memory, undefined
Language: c++

## How OSS-Fuzz builds this project (projects/libavif/build.sh)

```bash
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

bash tests/oss-fuzz/build.sh

# show contents of $OUT/ for sanity checking
find $OUT/
```

## Project configuration: https://github.com/google/oss-fuzz/tree/master/projects/libavif
