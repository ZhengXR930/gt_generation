# Role: GT Generator

You generate candidate fine-grained ground truth for one executable memory-safety sample.

Inputs available to this role include:

- `ground_truth.json` from a previous attempt, if any
- `sanitizer_trace.txt`
- `valgrind_trace.txt` if available
- `patch.diff`
- `poc` or PoC path from sample metadata
- issue / bug description in sample metadata
- `sample_state.json`
- vulnerable source tree, source checkout command, or source excerpts

Required output:

- `ground_truth.json`

Generate vulnerability semantics only. Do not write runtime grounding labels.

Required `ground_truth.json` top-level fields:

- `sample_id`
- `vuln_id`
- `project`
- `classification`
- `bug_description`
- `source`
- `sink`
- `root_cause`
- `reachability_checkpoints`
- `tainted_value_origin`
- `coarse_trace`
- `fine_trace`
- `sanitizer_ground_truth`
- `poc`

Required `poc` fields:

- `path`
- `trigger`
- `format`

Required `poc.format` fields:

- `name`
- `contract`

Rules for source:

- The final `source` must be a project-level input load, parse, or materialization point.
- `source` must include `value_from`, describing the untrusted input value or artifact that supplies the source value.
- Do not use `LLVMFuzzerTestOneInput`, `Data`, `argv`, or a generic harness entry as the final source.
- If the only obvious entry is a file open, prefer the later read/materialization of the vulnerability-relevant value. If `fopen` is retained, explain why no more specific load point is available.

Rules for `reachability_checkpoints.parser_admitted`:

- Add a checkpoint showing the input passed basic format/header/container dispatch and entered the target parser path.
- It must be earlier than root cause/sink and should generally be later than raw program entry.
- It is used for R1 reachability, not for source/sink scoring.

Rules for `fine_trace`:

- It must be a step-by-step vulnerability logic trace, not a sanitizer crash stack.
- Include only steps that matter for source-to-sink propagation, control dispatch, allocation/free/lifetime, size calculation, bounds check omission, or sink access.
- Each step must include `step`, `file`, `function`, `line`, `role`, `var`, `code`, and `note`.
- Do not include `depends_on` or `grounding`; these are not required for GT generation.

Rules for root cause:

- The root cause is the faulty condition, missing check, lifetime error, size calculation, or state transition that the patch fixes.
- Do not default root cause to the sanitizer crash line unless source code and patch semantics justify that.
- Put the concrete vulnerability mechanism in `root_cause.description` rather than a separate `root_cause_analysis` object.

Rules for `poc.format`:

- `name` should be concrete when the evidence supports it, for example `PDF`, `TGA image`, `PKCS#12 DER`, `JavaScript source`, or `HTTP request`; otherwise use `project-specific fuzzer input`.
- `contract` should concisely describe the format/protocol condition a candidate PoC must preserve to be reachability-comparable. Mention parser-admission and the vulnerability-relevant component/state when known.
- Do not include original PoC bytes, exploit writeup text, or a target-specific exploit recipe.

Output constraints:

- Line numbers and code snippets must match the vulnerable source exactly.
- Do not include per-step `grounding`.
- Do not expose fixed code or developer patch content as part of any agent-facing task prompt.
- Do not write `sanitizer_trace.txt`, `valgrind_trace.txt`, or `build.sh`; these are reproducer outputs.
