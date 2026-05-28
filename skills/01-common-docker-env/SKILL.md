---
name: common-docker-env
description: Build or reuse one shared Docker environment for memory-safety ground-truth generation. Use this before project checkout/build/reproduction when a bounded, reusable image with compilers, sanitizers, debuggers, and analysis tools is needed.
---

# Common Docker Environment

## Purpose

Provide one reusable Docker image for building and debugging many open-source C/C++ vulnerability samples without keeping one image per project.

The image should be general and intentionally stable. Do not keep expanding the base image for individual projects. Project-specific dependencies belong in each sample's `build.sh`, where they can be audited and replayed with that sample.

## Default Image

Use:

```text
gt-memory-env:latest
```

## Required Tooling

The image should include only baseline tooling:

- `git`, `curl`, `wget`, `ca-certificates`
- `build-essential`, `gcc`, `g++`, `clang`, `lld`, `llvm`
- `make`, `cmake`, `ninja-build`, `pkg-config`
- `autoconf`, `automake`, `libtool`, `m4`
- `gdb`, `lldb`, `valgrind`
- Python 3 and common packaging tools
- Common compression/archive tools
- A small set of common development headers that are broadly useful across projects

## Usage Rules

- Reuse this image across samples.
- Mount dataset and work directories from the host.
- Do not commit project-specific containers as images.
- Do not add one-off project dependencies to the Dockerfile unless they are broadly useful across many samples.
- Install sample-specific packages at the beginning of `gt_results/<sample_id>/build.sh`.
- Do not store large source trees or build outputs inside Docker volumes unless explicitly requested.
- Prefer host-mounted `work/<sample_id>/` so cleanup is transparent.

## Expected Output

After this skill runs, `docker image inspect gt-memory-env:latest` should succeed.

If the image cannot be built, set failure type `docker_env_failed` in `sample_state.json`.
