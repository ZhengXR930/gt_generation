#!/bin/bash -eu
# Runtime-stable OSS-Fuzz wrapper for libhevc.
rm -rf "$SRC/libhevc/fuzzer"
cp -a /gt/runtime_support/libhevc_fuzzer_overlay "$SRC/libhevc/fuzzer"
sed -i 's/IV_ARCH_T/IVD_ARCH_T/g' "$SRC/libhevc/fuzzer/hevc_dec_fuzzer.cpp"
: > "$SRC/hevc_dec_fuzzer_seed_corpus.zip"
"$SRC/libhevc/fuzzer/ossfuzz.sh"
