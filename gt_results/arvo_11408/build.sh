#!/bin/bash
# Reproducer for arvo_11408 (OSS-Fuzz issue 11408, openvswitch/ovs, CWE-415 Heap-double-free)
#
# ARVO fast path: the target is ALREADY built with AddressSanitizer inside the
# prebuilt image n132/arvo:11408-vul. Do NOT clone or rebuild the ovs repo.
# `/bin/arvo run` executes the prebuilt fuzzer (ofctl_parse_target) on /tmp/poc.
#
# This reproduces the double-free in minimatch_destroy (lib/match.c:1783),
# reached via ofctl_parse_flow (tests/oss-fuzz/ofctl_parse_target.c).
set -euo pipefail

IMAGE="n132/arvo:11408-vul"

# Reproduce the vulnerable crash and capture the full ASan trace.
docker run --rm --entrypoint /bin/bash "${IMAGE}" -c '/bin/arvo run' > sanitizer_trace.txt 2>&1 || true

echo "Reproduction complete. See sanitizer_trace.txt for the AddressSanitizer double-free trace."
