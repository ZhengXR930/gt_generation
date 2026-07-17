#!/bin/bash
# ARVO fast path: target pre-built with AddressSanitizer inside the local image.
# /bin/arvo run executes the prebuilt fuzzer (fuzzer-api) on /tmp/poc.
docker run --rm --entrypoint /bin/bash n132/arvo:32142-vul -c '/bin/arvo run'
