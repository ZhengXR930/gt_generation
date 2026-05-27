---
name: build-dual-instrumentation
description: Build two vulnerable-project variants for memory-safety reproduction: one sanitizer-oriented build and one Valgrind/debug-oriented build. Use this after the vulnerable checkout and target binary have been identified.
---

# Build Dual Instrumentation

## Purpose

Compile the vulnerable project twice:

- A sanitizer build for ASan/MSan/UBSan-style traces.
- A Valgrind-friendly debug build without sanitizer instrumentation.

The goal is reproducible evidence, not distribution-quality packaging.

## Inputs

- `work/<sample_id>/src`
- Candidate target binary or harness from the preparation stage.
- Sample PoC/PoV artifact.
- Project build files.
- Common Docker image.

## Build Requirements

Create and preserve `gt_results/<sample_id>/build.sh`. This script must be sufficient for a reviewer to rebuild both variants after recloning the vulnerable commit.

The script should:

- Record environment variables.
- Record configure/cmake/make commands.
- Build sanitizer and Valgrind variants in separate directories.
- Use debug symbols.
- Avoid destructive changes to source unless documented.

## Sanitizer Build

Prefer Clang with:

```text
-g -O1 -fno-omit-frame-pointer
```

Use the sanitizer matching the issue when known:

- ASan for out-of-bounds, use-after-free, double-free, invalid-free.
- MSan for uninitialized memory if practical.
- UBSan only as an auxiliary signal, not as a replacement for memory-safety traces.

## Valgrind Build

Prefer:

```text
-g -O0 -fno-omit-frame-pointer
```

Do not enable ASan/MSan in the Valgrind build.

## Outputs

- `gt_results/<sample_id>/build.sh`
- `work/<sample_id>/build_sanitizer/`
- `work/<sample_id>/build_valgrind/`
- Build command summaries in `generation.log`

## Failure Conditions

Use these failure types:

- `dependency_missing`
- `build_config_failed`
- `sanitizer_build_failed`
- `valgrind_build_failed`
- `needs_human_review`

