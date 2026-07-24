# Memory-Safety Ground Truth Dataset

This dataset stores reproducible memory-safety vulnerability samples and the artifacts needed to generate fine-grained ground truth.

## Selected 1,000-sample corpus

`selected_1000.json` is the canonical manifest. Its composition is fixed:

- `base`: 800 samples from ARVO (683) and SEC-bench (117).
- `new_diverse`: 200 unique public vulnerability IDs from NIST NVD, OSV/OSS-Fuzz, OSV Git, and GitHub Advisory Database. This group excludes ARVO and SEC-bench and is checked against the local CyberGym task catalog.

Every selected record has a non-empty issue description and a local patch that produces a non-empty result with `git apply --numstat`. Every non-ARVO record also has a non-empty local PoC/evidence asset. `poc_runnable` distinguishes a locally normalized runnable trigger from a public reproducer or dataset testcase asset; asset presence alone must not be interpreted as successful local reproduction. ARVO PoC availability relies on the ARVO dataset contract, as requested, and is not re-downloaded here.

Frozen dataset files:

- `selected_1000.json`: all selected samples and asset paths.
- `selection_summary.json`: aggregate composition and coverage.
- `selection_audit.json`: exclusions, failures, and qualification checks.
- `arvo/<id>/patch.diff`: patches for all 683 selected ARVO samples.
- `pocs/<sample_id>/`: one patch and one PoC/evidence asset for each of the 317 non-ARVO samples.

## Sample Admission Rules

Selection never compiles a project and never pulls an image. Two independent rules bound the local build cost:

- **Static project exclusion.** These projects are excluded outright on known source/dependency/build footprint, regardless of image size: `arrow`, `binutils`, `binutils-gdb`, `cryptofuzz`, `dawn`, `duckdb`, `envoy`, `ffmpeg`, `firebase-ios-sdk`, `gdal`, `grpc`, `gstreamer`, `icu`, `imagemagick`, `libjxl`, `mongo`, `opencv`, `openimageio`, `pcl`, `perfetto`, `postgis`, `qemu`, `serenity`, `skia`, `tesseract`, `wasmedge`, `wireshark`.
- **Image size proxy.** The local expanded budget is 10 GiB. Because expanded size is unknown before a pull, Docker Hub's compressed `full_size` for `n132/arvo:<id>-vul` must be `<= 4 GiB`. This is deliberately conservative: `arvo_11291` measures ~5.33 GB compressed against ~14.6 GB expanded. In practice only 14 of 1810 sized candidates exceeded the cap, so the static list does most of the filtering.

ARVO replacement samples additionally require a public sanitizer crash output, a patch that touches non-test source files, and a vulnerable commit resolvable as the fix commit's first parent, with at most 10 replacements per project.

## Sanitizer Trace Difficulty

`trace_difficulty` and `trace_unique_depth` record how demanding a sample's public sanitizer trace is, so the corpus is not silently dominated by shallow crashes.

- The metric is the number of **distinct project functions** on the primary crash stack, taken from ARVO's public `crash_output` (`arvo.db` v3.0.0).
- libfuzzer/LLVM/compiler-rt/libc frames and `LLVMFuzzerTestOneInput` are excluded, matching the unscored-harness-boundary rule used for `source` above.
- Frames are counted as distinct symbol+file pairs, so recursion does not inflate depth. One `moddable` case reaches 498 raw frames across only 2 files and scores as its 24 distinct functions.
- Buckets: `easy` `<=3`, `medium` `4-8`, `hard` `>=9`. `unknown` means no public crash output exists for that record; it does not mean easy.

ARVO records: 211 hard, 203 medium, 55 easy, 214 unknown. `new_diverse` and SEC-bench records have no ARVO crash output and are all `unknown`. When heavy projects were excluded, the removed samples skewed hard (55% hard), so their replacements were selected to match that mix rather than by smallest image, which would have made the corpus easier.

## Commit Provenance

`vulnerable_commit` is the fix commit's first parent. 991 of 1000 records carry a complete commit pair; the resolution was cross-validated against the GitHub API. The remaining 9 ARVO records are listed in `selection_audit.json` under `commit_provenance.unresolved`: ARVO metadata names a fix commit that does not exist in the stated repository, or the host refuses fetch-by-SHA. Their patch diffs are still valid.

The designated 200 new samples are the records in `selected_1000.json` whose `selection_group` is `new_diverse`; a second duplicate manifest is intentionally not stored.
Candidate pools and the one-off selection script are intentionally omitted now that the corpus is frozen.

## Ground Truth Semantics

Ground-truth JSON files use shared trace semantics across all samples, so individual `ground_truth.json` files do not repeat this policy.

- `coarse_trace` is a function-level control-flow summary. It should align with the key project frames in the observed sanitizer crash stack, while omitting incidental library/runtime frames when they do not help explain the vulnerability.
- `fine_trace` is a statement-level vulnerability and data-flow trace, not a literal call stack. It should recover the attacker-controlled source, propagation or state transitions, root cause, and sink. It may omit stack frames that do not transform attacker-controlled state.
- `sanitizer_ground_truth.crash_stack` is the observed runtime crash stack used for cross-validation. It is the control-stack evidence, not a replacement for `fine_trace`.
- `source` is the first project-code statement that actually consumes attacker-controlled input and creates vulnerability-relevant data or state. It should be a parser/load/read/materialization point, not a generic harness entry or an arbitrary crash-stack frame. For libFuzzer/OSS-Fuzz samples, `LLVMFuzzerTestOneInput` is treated as an unscored test boundary; the scored source should be the project parser or helper statement that reads `data,size` into a length, count, object, ownership/lifetime state, dispatch key, or equivalent state used by the vulnerable path.
- `sink` is the memory-safety operation where the invalid access/free/use occurs, or the closest project-code statement responsible for the sanitizer-observed invalid memory operation. It must be cross-checkable against `sanitizer_ground_truth.crash_location` or an equivalent detector location.
- `source.value_from` records the untrusted input value or artifact that supplies the source, such as a PoC file path, fuzzer-provided buffer, stream, network packet, or parser input object.
- `tainted_value_origin` is the first concrete vulnerability-relevant value or state derived from untrusted input, such as a parsed length, count, index, pointer, lifetime/ownership state, or type/dispatch tag.
- `poc.format` describes the minimal input format/protocol contract for PoC reachability evaluation. It should name the format and briefly state the parser-admission or vulnerability-relevant condition a candidate PoC must preserve, without embedding oracle PoC bytes or target exploit writeup material.
- Runtime/repository validation evidence belongs in validation artifacts. The GT generator should not be required to write per-step `grounding` labels.

Per-sample GT files should not include a `harness_context` field. Harness and OSS-Fuzz context belongs in preserved artifacts outside `ground_truth.json`, and evaluation workspaces should hide benchmark-added harness files from the tested agent unless they are part of the intended task context.

## Grounding Evidence

Grounding provenance is interpreted as follows:

- `sanitizer_stack`: the step location appears in the authoritative sanitizer crash stack.
- `allocation_context`: the step exactly matches the sanitizer allocation context.
- `free_context`: the step exactly matches the sanitizer free context.
- `gdb_watchpoint`: structured GDB watchpoint JSON observed the watched variable at the step location.
- `gdb_coverage`: structured GDB instrumentation observed the step location during PoC execution. This is precision evidence only, not recall.
- `patch`: the step is on or near a patch hunk line with a patch-groundable role.
- `asserted`: no runtime or patch artifact directly grounded the step; this requires lower confidence or human review when critical.

GDB watchpoints are used only for precision and data-flow grounding. They do not provide recall guarantees and are not a substitute for backward slicing.
