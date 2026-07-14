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

REQUIRED SCHEMA — your `ground_truth.json` MUST pass `python3 -m gt_toolkit validate
{result_dir}/ground_truth.json` before you finish (run it yourself and fix every error;
this stage is REJECTED if validation fails). It requires ALL of these top-level keys:

- `sample_id` (string), `vuln_id` (string)
- `project` = object `{id, repo, vulnerable_commit, fixed_commit}` (from sample metadata)
- `classification` = object `{class, cwe}` (bug class + CWE, e.g. `{"class":"heap-buffer-overflow","cwe":"CWE-125"}`)
- `bug_description` = object `{original, original_source, normalized}` (from the sample's issue/report)
- `source`, `sink`, `root_cause`, `tainted_value_origin` = node objects (file/function/line/var/…)
- `coarse_trace` = array of steps (function-level call chain from the crash stack); each step is an object
- `fine_trace` = array of the fine-grained nodes (source→materialization→root_cause→sink + alloc/free)
- `sanitizer_ground_truth` = object with at least `{detector, trace_format, crash_type, crash_location}`
  (fill from `sanitizer_trace.txt`: detector=address/memory, crash_type, the crashing file:line)
- `poc` = object referencing the PoC (path/size/…)

Copy `project`/`bug_description`/`classification` fields straight from the sample metadata
and the sanitizer trace — do NOT leave them empty or as strings. Self-validate, fix, repeat.

Source of truth for line numbers (CRITICAL):

- Every `file`/`line` in the GT MUST be grounded in the SAME source that was built and
  reproduced — never a fresh commit checkout (line numbers drift between them).
- The `00_prepare` stage has ALREADY staged everything you need — do NOT pull images or
  run docker. Read line numbers directly from the pre-extracted source tree at
  `<result_dir>/_work/src` and the crash from `<result_dir>/sanitizer_trace.txt`
  (both are on disk). The vulnerable/fix images are already pulled locally if you need
  to spot-check, but you should not need to. NEVER `docker pull` / `docker create` here —
  that is the prepare stage's job and pulling in this stage is what previously caused
  failures.
- Write each GT `file` as the repo-relative path EXACTLY as it appears in
  `sanitizer_trace.txt` (e.g. `libarchive/libarchive/archive_read_support_format_rar5.c`,
  not a shortened `libarchive/...`), so evaluation path matching is exact.

Cleanup before you finish (disk hygiene):

- Delete the `_work/` source copy and ANY `.git` directory under the result directory
  (`find <result_dir> -name .git -type d -prune -exec rm -rf {} +`).
- Keep only the canonical artifacts (`ground_truth.json`, and the reproducer outputs).
  Do not leave stray docker-cp'd source files in the result directory.

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
  Keep it FINE-GRAINED — do not collapse it to only the anchor checkpoints; the
  intermediate propagation steps are part of GT quality.
- Include the steps that matter for source-to-sink propagation, control dispatch,
  allocation/free/lifetime, size calculation, bounds check omission, or sink access.
- Each step must include `step`, `file`, `function`, `line`, `role`, `var`, `code`, `note`,
  and (except the first) `depends_on`.
- Do NOT include `grounding` (per-step evidence labels are intentionally excluded).

The `role` field names each step's semantics. Use this vocabulary:

- endpoint roles (the two artifact-grounded anchors): `source`, `sink`.
- between roles (the reasoning that connects the anchors): `tainted_value_materialization`,
  `dispatch`/`indirect_call`, `alloc`/`unsafe_allocation`, `root_cause`, `free`/`invalid_free`.
- connective roles (help explain propagation): `tainted_read`, `propagate`, `alias`,
  `bounds_state`, `lifetime_state`, `entry`.

`depends_on` is the trace's EDGE set (the source->sink association). Nodes are the
steps; edges connect them. Data flow alone is not enough — encode three edge types.
CRITICAL: `via` is code (a symbol, an expression, or a relation keyword), never a
natural-language sentence. Explanation goes in the step's `note`, which is not scored.

- `data`: value provenance — `via` is the variable —
  `{"on": <step>, "type": "data", "via": "len"}`.
- `control`: the guard that makes the sink reachable, or the missing check that is the
  root cause. `via` is the guard PREDICATE EXPRESSION verbatim from the patch/source
  (say "missing" vs "taken" in `note`) —
  `{"on": <step>, "type": "control", "via": "out + count > end"}`.
- `order`: temporal happens-before for lifetime bugs (UAF/double-free/uninit). `via` is
  a relation keyword from {free_before_use, double_free, use_before_init,
  use_after_return, use_after_scope}; add `obj` only if the pointer is not already a
  step variable — `{"on": <step>, "type": "order", "via": "free_before_use"}`.

Per bug class the essential edges are: OOB/overflow = data + control(missing check);
UAF = data + order(free_before_use) + control(path); double-free = order(two frees);
uninit = control(skipped init) + data. The `order`/lifetime checkpoints (alloc/free/use)
should be taken from `sanitizer_ground_truth` (allocation_stack / free_stack / crash_stack),
not invented.

Do NOT mark `key` on any step. Selecting which nodes are the scored invariants is NOT
this role's job — the Runtime Validator (stage 04) selects them deterministically from
the artifacts (crash trace -> sink; patch -> root_cause; sanitizer alloc/free/crash
stacks -> lifetime order points; the input load -> source; the faulting value's
materialization) and then VERIFIES them by instrumentation. Your job here is only to
author a complete, correct `fine_trace` (nodes + typed `depends_on` edges) with exact
line numbers and code; make sure the artifact-anchored points (source, the value
materialization, root_cause, sink, and any alloc/free for lifetime bugs) are present as
steps with their edges so stage 04 can find and verify them.

Completeness for batch production: produce a COMPLETE GT for every sample, filling
even the SOFT control/order edges yourself. Do not leave anchor checkpoints or edges
blank pending review — the dataset is verified by later sampling, not per-sample
blocking. If a soft control edge is uncertain, still author your best judgement and
mark uncertainty in `note`.

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
