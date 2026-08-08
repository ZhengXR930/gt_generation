FROM gt-memory-env:latest

# The Reward controller uses Docker SDK for OpenHands runtime lifecycle, while
# the ARVO/GDB verifier intentionally invokes the Docker CLI.  Keep both in the
# isolated controller image so an unavailable CLI is never reported as a PoC
# failure.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*
