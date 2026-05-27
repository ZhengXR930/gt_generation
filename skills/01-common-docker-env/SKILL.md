---
name: common-docker-env
description: Build or reuse one shared Docker environment for memory-safety ground-truth generation. Use this before project checkout/build/reproduction when a bounded, reusable image with compilers, sanitizers, debuggers, and analysis tools is needed.
---

# Common Docker Environment

## Purpose

Provide one reusable Docker image for building and debugging many open-source C/C++ vulnerability samples without keeping one image per project.

The image should be general. Project-specific dependencies may still be installed inside a temporary container or handled by build scripts, but the base image must contain common compilers, debuggers, build tools, and runtime analysis tools.

## Default Image

Use:

```text
gt-memory-env:latest
```

## Required Tooling

The image should include:

- `git`, `curl`, `wget`, `ca-certificates`
- `build-essential`, `gcc`, `g++`, `clang`, `lld`, `llvm`
- `make`, `cmake`, `ninja-build`, `pkg-config`
- `autoconf`, `automake`, `libtool`, `m4`
- `gdb`, `lldb`, `valgrind`
- Python 3 and common packaging tools
- Common compression/archive tools
- Common development headers for parser/media/security projects when available

## Usage Rules

- Reuse this image across samples.
- Mount dataset and work directories from the host.
- Do not commit project-specific containers as images.
- Do not store large source trees or build outputs inside Docker volumes unless explicitly requested.
- Prefer host-mounted `work/<sample_id>/` so cleanup is transparent.

## Expected Output

After this skill runs, `docker image inspect gt-memory-env:latest` should succeed.

If the image cannot be built, set failure type `docker_env_failed` in `sample_state.json`.

