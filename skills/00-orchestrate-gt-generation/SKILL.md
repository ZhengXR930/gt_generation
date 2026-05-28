---
name: orchestrate-gt-generation
description: Orchestrate a disk-bounded, resumable ground-truth generation pipeline for one or more memory-safety benchmark samples. Use this when coordinating checkout, build, reproduction, trace analysis, GT generation, instrumentation coverage, validation, cleanup, and final artifact layout.
---

# Orchestrate GT Generation

## Purpose

Run the full fine-grained ground-truth pipeline in a deterministic, resumable way while keeping storage bounded.

This skill does not perform deep vulnerability reasoning itself. It coordinates the stage-specific skills, records state, logs every important decision, and ensures the final per-sample artifacts are clean and stable.

## Inputs

- Dataset manifest entry from `all_samples.json`.
- Sample artifact directory, usually `final_dataset/pocs/<sample_id>/`.
- Output root, usually `gt_results/`.
- Temporary work root, usually `work/`.
- Common Docker image name, usually `gt-memory-env:latest`.

## Final Artifacts

Only keep these files under `gt_results/<sample_id>/`:

- `build.sh`
- `sanitizer_trace.txt`
- `valgrind_trace.txt`
- `ground_truth.json` or `partial_ground_truth.json`
- `sample_state.json`
- `generation.log`

Temporary checkout and build directories must be deleted after successful completion or after a terminal failure unless the user explicitly asks to preserve them.

## Workspace Layout

Use this layout:

```text
gt_results/<sample_id>/
work/<sample_id>/
```

Within `work/<sample_id>/`, stage-specific skills may create:

```text
src/
build_sanitizer/
build_valgrind/
instrumentation/
tmp/
```

These work directories are disposable.

## State Model

Maintain `gt_results/<sample_id>/sample_state.json` throughout the run. It must be updated after every stage.

Required top-level fields:

```json
{
  "sample_id": "",
  "status": "not_started | running | completed | failed | needs_human_review",
  "current_stage": "",
  "completed_stages": [],
  "failure": {
    "stage": "",
    "type": "",
    "message": ""
  },
  "artifacts": {
    "build_script": "build.sh",
    "sanitizer_trace": "sanitizer_trace.txt",
    "valgrind_trace": "valgrind_trace.txt",
    "ground_truth": "ground_truth.json",
    "generation_log": "generation.log"
  },
  "reproduction": {
    "sanitizer_crash_observed": false,
    "valgrind_crash_observed": false,
    "matches_issue_description": false
  },
  "coverage": {
    "checked": false,
    "covered_gt_locations": 0,
    "missing_gt_locations": 0
  },
  "validation": {
    "schema_valid": false,
    "source_sink_valid": false,
    "root_cause_matches_patch": false,
    "requires_human_review": true
  },
  "cleanup": {
    "source_deleted": false,
    "build_deleted": false
  }
}
```

## Stage Order

Run stages in this order:

1. `common-docker-env`
2. `prepare-vulnerable-project`
3. `build-dual-instrumentation`
4. `reproduce-crash`
5. `trace-triage`
6. `generate-fine-grained-gt`
7. `instrumentation-coverage-check`
8. `validate-ground-truth`
9. Cleanup temporary checkout and build directories.

Resume from the first incomplete stage. Do not redo completed stages unless their inputs changed or an artifact is missing.

## Failure Taxonomy

Use one of these failure types in `sample_state.json`:

- `docker_env_failed`
- `clone_failed`
- `checkout_failed`
- `dependency_missing`
- `build_config_failed`
- `sanitizer_build_failed`
- `valgrind_build_failed`
- `trigger_missing`
- `trigger_failed_no_crash`
- `trigger_crash_mismatch`
- `trace_too_shallow`
- `source_uncertain`
- `sink_uncertain`
- `data_flow_incomplete`
- `instrumentation_failed`
- `instrumentation_not_covered`
- `gt_schema_invalid`
- `validation_failed`
- `needs_human_review`

## Logging

Append all commands, decisions, failures, and evidence summaries to `generation.log`.

Do not rely on separate intermediate summary JSON files for final review. If a stage produces useful intermediate observations, summarize them in `generation.log` and copy the important status into `sample_state.json`.

When a build requires packages beyond the common Docker image, record the package names in `build.sh` and mention them in `generation.log`. Do not mutate the shared Docker image for a single project.

## Cleanup Rule

After a completed or terminal failed run:

- Keep final artifacts under `gt_results/<sample_id>/`.
- Delete `work/<sample_id>/src`.
- Delete `work/<sample_id>/build_sanitizer`.
- Delete `work/<sample_id>/build_valgrind`.
- Delete large temporary files.

Never delete the dataset sample directory under `final_dataset/pocs/<sample_id>/`.
