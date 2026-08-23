#!/usr/bin/env bash
set -euo pipefail

cd "${SRC:?}"

# Upstream builder checks out libhevc as $SRC/libhevc and places the seed
# corpus at $SRC/hevc_dec_fuzzer_seed_corpus.zip.
ln -sfn . libhevc

if [ ! -f hevc_dec_fuzzer_seed_corpus.zip ]; then
  curl -L --retry 3 -o hevc_dec_fuzzer_seed_corpus.zip \
    https://storage.googleapis.com/android_media/external/libhevc/fuzzer/hevc_dec_fuzzer_seed_corpus.zip
fi
