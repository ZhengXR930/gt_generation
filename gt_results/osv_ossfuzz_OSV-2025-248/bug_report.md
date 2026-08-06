================= Bug Report (1/1) ==================
## Source: OSS-Fuzz
## Assembled from: this sample's OSS-Fuzz record, its crash record, the
## project's configuration in google/oss-fuzz, and the harness sources
## present in this checkout. Not a verbatim upstream report; the fields
## under 'Reproduction target' and 'Crash record' are the sample's own
## and are authoritative.
## URL: https://bugs.chromium.org/p/oss-fuzz/issues/detail?id=407448850
## Project: clamav

## Reproduction target
Fuzzing Engine: libFuzzer
Fuzz Target: clamav_dbload_WDB_fuzzer
Job Type: libfuzzer_asan_clamav
Sanitizer: address (ASAN) -- build with -fsanitize=address
ClusterFuzz testcase: clusterfuzz-testcase-minimized-clamav_dbload_WDB_fuzzer-4514655573966848

Build clamav_dbload_WDB_fuzzer and run it as `clamav_dbload_WDB_fuzzer <poc>`. The PoC is a libFuzzer testcase; feeding it to a command line tool reproduces nothing.

## Crash record
OSS-Fuzz report: https://bugs.chromium.org/p/oss-fuzz/issues/detail?id=407448850

```
Crash type: Heap-buffer-overflow READ 1
Crash state:
cli_bm_addpatt
add_hash
load_regex_matcher
```

## Harness sources in this checkout
No file in this checkout defines clamav_dbload_WDB_fuzzer. Its harness most likely lives in the google/oss-fuzz project directory (see the build.sh below), not in this repository.

Files here that define LLVMFuzzerTestOneInput:
  - fuzz/clamav_dbload_fuzzer.cpp
  - fuzz/clamav_scanfile_fuzzer.cpp
  - fuzz/clamav_scanmap_fuzzer.cpp
  - fuzz/standalone_fuzz_target_runner.cpp

## Project configuration (google/oss-fuzz)
Sanitizers this project is fuzzed under: address, undefined
Targets build.sh installs into $OUT: clamav_dbload_, clamav_scanfile_, clamav_scanmap_
Language: c++

## How OSS-Fuzz builds this project (projects/clamav/build.sh)

```bash
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

set -ex
export GIT_DISCOVERY_ACROSS_FILESYSTEM=1

#
# Build the library.
#
rm -rf ${SRC}/build
mkdir -p ${SRC}/build
cd ${SRC}/build

#
# Run ./configure
#
export CLAMAV_DEPENDENCIES=/mussels/install
cmake ${SRC}/clamav \
    -DENABLE_FUZZ=ON                                                   \
    -DHAVE_MMAP=OFF                                                    \
    -DJSONC_INCLUDE_DIR="$CLAMAV_DEPENDENCIES/include/json-c"          \
    -DJSONC_LIBRARY="$CLAMAV_DEPENDENCIES/lib/libjson-c.a"             \
    -DENABLE_JSON_SHARED=OFF                                           \
    -DBZIP2_INCLUDE_DIR="$CLAMAV_DEPENDENCIES/include"                 \
    -DBZIP2_LIBRARY_RELEASE="$CLAMAV_DEPENDENCIES/lib/libbz2.a"        \
    -DOPENSSL_ROOT_DIR="$CLAMAV_DEPENDENCIES"                          \
    -DOPENSSL_INCLUDE_DIR="$CLAMAV_DEPENDENCIES/include"               \
    -DOPENSSL_CRYPTO_LIBRARY="$CLAMAV_DEPENDENCIES/lib/libcrypto.a"    \
    -DOPENSSL_SSL_LIBRARY="$CLAMAV_DEPENDENCIES/lib/libssl.a"          \
    -DZLIB_LIBRARY="$CLAMAV_DEPENDENCIES/lib/libssl.a"                 \
    -DLIBXML2_INCLUDE_DIR="$CLAMAV_DEPENDENCIES/include/libxml2"       \
    -DLIBXML2_LIBRARY="$CLAMAV_DEPENDENCIES/lib/libxml2.a"             \
    -DPCRE2_INCLUDE_DIR="$CLAMAV_DEPENDENCIES/include"                 \
    -DPCRE2_LIBRARY="$CLAMAV_DEPENDENCIES/lib/libpcre2-8.a"            \
    -DZLIB_INCLUDE_DIR="$CLAMAV_DEPENDENCIES/include"                  \
    -DZLIB_LIBRARY="$CLAMAV_DEPENDENCIES/lib/libz.a"                   \
    -DCMAKE_INSTALL_PREFIX="install"

# Build libclamav and the fuzz targets
make -j$(nproc)
cp ./fuzz/clamav_* ${OUT}/.

#
# Collect the fuzz corpora.
#

# `scanfile` & `scanmap`
# ----------
mkdir ${SRC}/all-scantype-seeds
git clone --depth 1 https://github.com/Cisco-Talos/clamav-fuzz-corpus.git $SRC/clamav-fuzz-corpus

for type in ARCHIVE MAIL OLE2 PDF HTML PE ELF SWF XMLDOCS HWP3; do
    # Prepare seed corpus for the type-specific fuzz targets.
    zip ${OUT}/clamav_scanfile_${type}_fuzzer_seed_corpus.zip ${SRC}/clamav-fuzz-corpus/scantype/${type}/*
    zip ${OUT}/clamav_scanmap_${type}_fuzzer_seed_corpus.zip ${SRC}/clamav-fuzz-corpus/scantype/${type}/*

    # Prepare dictionary for the type-specific fuzz targets (may not exist for all types).
    cp ${SRC}/clamav-fuzz-corpus/scantype/${type}.dict ${OUT}/clamav_scanfile_${type}_fuzzer.dict 2>/dev/null || :
    cp ${SRC}/clamav-fuzz-corpus/scantype/${type}.dict ${OUT}/clamav_scanmap_${type}_fuzzer.dict 2>/dev/null || :

    # Copy seeds for the generic fuzz target.
    cp ${SRC}/clamav-fuzz-corpus/scantype/${type}/* ${SRC}/all-scantype-seeds/
done

# Add weird files
git clone --depth=1 https://github.com/corkami/pocs
find ./pocs/ -type f -print0 | xargs -0 -I % mv -f % ${SRC}/all-scantype-seeds/

# Prepare seed corpus for the generic fuzz target.
cp ${SRC}/clamav-fuzz-corpus/scantype/other/* ${SRC}/all-scantype-seeds/
zip ${OUT}/clamav_scanfile_fuzzer_seed_corpus.zip ${SRC}/all-scantype-seeds/*
zip ${OUT}/clamav_scanmap_fuzzer_seed_corpus.zip ${SRC}/all-scantype-seeds/*
rm -r ${SRC}/all-scantype-seeds

# `dbload`
# --------
for type in CDB CFG CRB FP FTM HDB HSB IDB IGN IGN2 LDB MDB MSB NDB PDB WDB YARA; do
    # Prepare seed corpus for the type-specific fuzz targets.
    zip ${OUT}/clamav_dbload_${type}_fuzzer_seed_corpus.zip ${SRC}/clamav-fuzz-corpus/database/${type}/*

    # Prepare dictionary for the type-specific fuzz targets (may not exist for all types).
    cp ${SRC}/clamav-fuzz-corpus/database/${type}.dict ${OUT}/clamav_dbload_${type}_fuzzer.dict 2>/dev/null || :
done
```

## Project configuration: https://github.com/google/oss-fuzz/tree/master/projects/clamav
