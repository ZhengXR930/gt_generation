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

- Start with a project dependency section.
- Install only the extra apt packages needed by this project.
- Record environment variables.
- Record configure/cmake/make commands.
- Build sanitizer and Valgrind variants in separate directories.
- Use debug symbols.
- Avoid destructive changes to source unless documented.

Use this pattern near the top of `build.sh`:

```bash
install_project_deps() {
  local deps=(
    # Example: ruby rake bison libpcre2-dev
  )
  if [ "${#deps[@]}" -gt 0 ]; then
    apt-get update
    apt-get install -y --no-install-recommends "${deps[@]}"
    rm -rf /var/lib/apt/lists/*
  fi
}
```

Keep the dependency list explicit even when it is short. Do not hide project-specific dependencies by baking them into `gt-memory-env:latest`.

## Sanitizer Build

Prefer GCC with:

```text
-g -O1 -fno-omit-frame-pointer
```

Use Clang only when its sanitizer runtime is available in the active Docker architecture. If Clang sanitizer linking fails, switch the sample build recipe to GCC rather than expanding the common Docker image for that single project.

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
