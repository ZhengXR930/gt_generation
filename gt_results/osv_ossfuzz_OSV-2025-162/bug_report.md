================= Bug Report (1/1) ==================
## Source: OSS-Fuzz
## Assembled from: this sample's OSS-Fuzz record, its crash record, the
## project's configuration in google/oss-fuzz, and the harness sources
## present in this checkout. Not a verbatim upstream report; the fields
## under 'Reproduction target' and 'Crash record' are the sample's own
## and are authoritative.
## URL: https://bugs.chromium.org/p/oss-fuzz/issues/detail?id=398067543
## Project: net-snmp

## Reproduction target
Fuzzing Engine: libFuzzer
Fuzz Target: snmp_parse_args_fuzzer
Job Type: libfuzzer_asan_net-snmp
Sanitizer: address (ASAN) -- build with -fsanitize=address
ClusterFuzz testcase: clusterfuzz-testcase-minimized-snmp_parse_args_fuzzer-6384743772127232

Build snmp_parse_args_fuzzer and run it as `snmp_parse_args_fuzzer <poc>`. The PoC is a libFuzzer testcase; feeding it to a command line tool reproduces nothing.

## Crash record
OSS-Fuzz report: https://bugs.chromium.org/p/oss-fuzz/issues/detail?id=398067543

```
Crash type: Heap-buffer-overflow READ 8
Crash state:
snmp_in_options
netsnmp_parse_args
snmp_parse_args_fuzzer.c
```

## Harness sources in this checkout
snmp_parse_args_fuzzer is defined here:
  - testing/fuzzing/snmp_parse_args_fuzzer.c

Other targets in this repository:
  - testing/fuzzing/agentx_parse_fuzzer.c
  - testing/fuzzing/parse_octet_hint_fuzzer.c
  - testing/fuzzing/read_objid_fuzzer.c
  - testing/fuzzing/snmp_agent_e2e_fuzzer.c
  - testing/fuzzing/snmp_api_fuzzer.c
  - testing/fuzzing/snmp_config_fuzzer.c
  - testing/fuzzing/snmp_config_mem_fuzzer.c
  - testing/fuzzing/snmp_e2e_fuzzer.c
  - testing/fuzzing/snmp_mib_fuzzer.c
  - testing/fuzzing/snmp_parse_fuzzer.c
  - testing/fuzzing/snmp_parse_oid_fuzzer.c
  - testing/fuzzing/snmp_pdu_parse_fuzzer.c
  - testing/fuzzing/snmp_print_var_fuzzer.c
  - testing/fuzzing/snmp_scoped_pdu_parse_fuzzer.c
  - testing/fuzzing/snmp_transport_fuzzer.c

## How OSS-Fuzz builds this project (projects/net-snmp/build.sh)

```bash
#!/bin/bash -eu
# Copyright 2018 Google Inc.
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

# Globally disable leaks to let fuzzers continue.
export ASAN_OPTIONS="detect_leaks=0"
export CFLAGS="${CFLAGS} -Wno-error=declaration-after-statement"

# Configure and build Net-SNMP and the fuzzers.
export CC CXX CFLAGS CXXFLAGS SRC WORK OUT LIB_FUZZING_ENGINE
MODE=regular ci/build.sh

# Create dictionary and seeds
cp $SRC/mib.dict $OUT/snmp_mib_fuzzer.dict
zip $OUT/snmp_mib_fuzzer_seed_corpus.zip $SRC/net-snmp/mibs/*.txt
```

## Project configuration: https://github.com/google/oss-fuzz/tree/master/projects/net-snmp
