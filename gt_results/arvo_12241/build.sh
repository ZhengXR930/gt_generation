#!/bin/bash
# Reproducer build/run script for arvo_12241 (harfbuzz, CWE-415 Heap-double-free)
#
# ARVO fast path: the vulnerable target is ALREADY built with AddressSanitizer
# inside the prebuilt image n132/arvo:12241-vul. We do NOT clone or rebuild.
# `/bin/arvo run` executes the prebuilt fuzzer (hb-subset-fuzzer) on /tmp/poc.
#
# Target binary : /out/hb-subset-fuzzer
# Fuzzer        : hb-subset-fuzzer (libFuzzer)
# Sanitizer     : address (ASan)
# Vulnerable ref: d25a2f1496d13846ddaea123ac6fb9807dc5669a (harfbuzz)
set -euo pipefail

IMAGE="n132/arvo:12241-vul"

# Reproduce the crash and capture the full sanitizer report.
docker run --rm --entrypoint /bin/bash "${IMAGE}" -c '/bin/arvo run' > sanitizer_trace.txt 2>&1
