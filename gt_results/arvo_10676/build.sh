#!/usr/bin/env bash
set -euo pipefail
# ARVO fast path: the target is already built with AddressSanitizer inside the
# image, and the PoC is baked in at /tmp/poc. `/bin/arvo run` executes the
# prebuilt fuzzer on /tmp/poc. Do NOT clone or rebuild.
docker run --rm --entrypoint /bin/bash n132/arvo:10676-vul -c '/bin/arvo run'
