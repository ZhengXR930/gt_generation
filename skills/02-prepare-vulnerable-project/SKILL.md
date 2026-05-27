---
name: prepare-vulnerable-project
description: Prepare a vulnerable project workspace for one memory-safety sample. Use this to read sample metadata, clone the repository, checkout the vulnerable commit, inspect the project, and identify candidate build and trigger targets before compiling.
---

# Prepare Vulnerable Project

## Purpose

Create a clean source checkout for the vulnerable version and identify the likely target binary, harness, or fuzz target needed to reproduce the issue.

This skill does not compile. It prepares evidence for the build stage.

## Inputs

- Sample manifest row.
- `final_dataset/pocs/<sample_id>/`.
- `normalized_bug_description`.
- `original_bug_description`.
- `trigger.json`.
- `run.sh`.
- `poc` or PoV artifact.

## Required Actions

1. Create `work/<sample_id>/src`.
2. Clone `repo_url`.
3. Checkout `vulnerable_commit`.
4. Read `trigger.json`, `run.sh`, and the bug descriptions.
5. Inspect project structure and build system.
6. Identify candidate target binaries, harnesses, fuzz targets, or test tools.
7. Record observations in `generation.log`.

## Target Identification Guidance

Use all available evidence:

- Issue description or advisory text.
- Crash type and crash state.
- Fuzz target names from OSS-Fuzz or ARVO.
- SEC-bench `secb.sh` command.
- PoC file extension and content.
- Project build files and test harnesses.
- Patch diff only as oracle context, not as the sole basis for target choice.

## Outputs

The stage should leave:

- `work/<sample_id>/src/`
- Candidate target notes in `generation.log`
- A clear build plan for the next stage

Do not create final GT here.

## Failure Conditions

Use these failure types:

- `clone_failed`
- `checkout_failed`
- `trigger_missing`
- `needs_human_review`

