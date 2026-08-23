#!/usr/bin/env bash
set -euo pipefail

cd "${SRC:?}"

# The OSS-Fuzz recipe restores PcapPlusPlus, libpcap, and tcpdump as sibling
# directories under $SRC.  Recreate that layout for the single-repo runtime.
ln -sfn . PcapPlusPlus

if [ ! -d libpcap/.git ]; then
  git clone --depth 1 https://github.com/the-tcpdump-group/libpcap.git libpcap
fi

if [ ! -d tcpdump/.git ]; then
  git clone --depth 1 https://github.com/the-tcpdump-group/tcpdump.git tcpdump
fi
