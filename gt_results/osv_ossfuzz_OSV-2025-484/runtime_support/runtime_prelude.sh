#!/usr/bin/env bash
set -euo pipefail

cd "${SRC:?}"

# Match the OSS-Fuzz builder layout: project source is $SRC/ndpi, with
# libpcap/json-c tarballs expanded beside it.
ln -sfn . ndpi

if [ ! -f libpcap-1.9.1.tar.gz ]; then
  curl -L --retry 3 -o libpcap-1.9.1.tar.gz \
    https://www.tcpdump.org/release/libpcap-1.9.1.tar.gz
fi

if [ ! -d libpcap-1.9.1 ]; then
  tar -xzf libpcap-1.9.1.tar.gz
fi

if [ ! -f json-c-0.17-20230812.tar.gz ]; then
  curl -L --retry 3 -o json-c-0.17-20230812.tar.gz \
    https://github.com/json-c/json-c/archive/refs/tags/json-c-0.17-20230812.tar.gz
fi

if [ ! -d json-c-json-c-0.17-20230812 ]; then
  tar -xzf json-c-0.17-20230812.tar.gz
fi
