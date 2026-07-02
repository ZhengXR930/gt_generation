# Memory-Safety Ground Truth Dataset

This dataset stores reproducible memory-safety vulnerability samples and the artifacts needed to generate fine-grained ground truth.

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
