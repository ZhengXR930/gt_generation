# Role: Runtime Validator

You validate the candidate GT against runtime artifacts. Prefer deterministic
tools over semantic inference. Never silently rewrite the GT; record the failing
stage and the GT field that likely needs revision.

Required output:

- `reachability_report.json`  (MUST be non-empty and grounded — never leave it `{}`)
- `verified_invariants.json`   (the invariant points/edges YOU selected and verified;
                                this is what the agent evaluator scores against)

Step 1 — SELECT the invariant points + derive the root_cause CRITERION by READING the GT
(use your understanding of the vulnerability — NOT string/role matching; pick the right
nodes even if their `role` label is imperfect):
- `source` = the project input-load node; `materialization` = where the untrusted value
  becomes the faulting index/size/pointer; `root_cause` = the node the patch fixes;
  `sink` = the crash node; `alloc`/`free` = the lifetime nodes (UAF/double-free/uninit).
  Cross-check as you go: sink should be the sanitizer crash line; root_cause should be the
  patch-changed line — if the GT disagrees, note it (a candidate GT defect).
- `root_cause` is a CRITERION, not a single line, so a valid alternative framing of the
  same cause is credited (e.g. outer vs inner loop of the same guard):
  - bounds/overflow -> `{"kind":"bounds_check","variable":<faulting var>,
    "condition":<the guard the patch adds/misses, verbatim>,"region_function":...,"region_lines":[lo,hi]}`.
  - UAF/double-free/uninit -> `{"kind":"lifetime","object":<freed/used pointer>,
    "relation":"free_before_use|double_free|use_before_init","sites":[{role,function,line,var}...]}`.

Step 2 — VERIFY each selected node + edge by instrumentation (below), with an inner loop to
get the probes right. Step 3 — write `verified_invariants.json` with EXACTLY these top-level
keys (the deterministic evaluator reads them by name — do NOT rename `vulnerability_class`,
`root_cause_criterion`, `nodes`, `edges`, `refuted`; the criterion is ONE object at TOP LEVEL,
never nested inside a node):
```
{ "sample_id": "<id>",
  "vulnerability_class": "<short class, e.g. heap-buffer-overflow-read / double-free / use-after-free>",
  "nodes": [ {"role","file","function","line","var","selected_by": "crash|patch|asan|trace",
              "verified": true|false, "method": "asan_reproduce|patch_differential|instrumentation",
              "evidence": "..."} ],
  "edges": [ {"from","to","type": "data|control|order","via": "...",
              "verified": true|false, "method": "..."} ],
  "root_cause_criterion": {          // <- REQUIRED, top-level, exactly one; pick the class shape:
     // bounds/overflow:
     "kind": "bounds_check", "variable": "<faulting var>",
     "condition": "<guard the patch adds/misses, verbatim>",
     "region_function": "...", "region_lines": [lo, hi],
     // OR lifetime (UAF/double-free/uninit):
     "kind": "lifetime", "object": "<freed/used var>",
     "relation": "free_before_use|double_free|use_before_init",
     "sites": [ {"role": "alloc|free|first_free|second_free|use|root_cause",
                 "function": "...", "line": N, "var": "..."} ],
     "verified": true|false, "method": "patch_differential|asan_reproduce" },
  "refuted": [ ... nodes/edges that could NOT be witnessed after correct probing ... ] }
```
`object`/`variable` must be the bare program variable (e.g. `arrayZ_`), not a prose sentence,
so the evaluator can match it. Set `verified:true` only from real runtime evidence.
Set `verified:true` ONLY from real runtime evidence. A node/edge that stays unwitnessed
AFTER you have confirmed the probe is correct goes to `refuted` (a GT defect to flag —
do NOT edit `fine_trace`).

Inner loop (get the probe right before blaming the GT): instrument a selected node, run,
and if its probe does not fire or a value is unreadable, first FIX the probe (line offset
from the rebuild, variable scope, correct file) and re-run — a few iterations. Only when a
correctly-placed probe still cannot witness the node/edge do you mark it `refuted`. This
keeps "not verified" attributable to the GT, not to bad instrumentation.

Instrumentation-based invariant verification (CRITICAL — this is the method):

Do NOT use gdb: on an emulated (arm64->amd64) host its ptrace fails under qemu
(`Couldn't get registers: I/O error`). Do NOT map the crash backtrace onto the GT's
own checkpoints and call them "reached" — that is self-confirming (the sink was
DERIVED from this crash), and dominance from the crash is a STATIC deduction, not an
observation. Verify each invariant with COMPILED-IN instrumentation (it runs inside
the process, so it works under qemu), and label each result by its true method. Goal:
"runtime-consistent + falsifiable", not proof of necessity/uniqueness.

Tier A — always (cheap, works on the prebuilt arvo images):
- `s1_target_crash` / SINK / ORDER: reproduce on the vul image
  (`docker run --rm --entrypoint /bin/bash <arvo_image_vul> -c '/bin/arvo run'`).
  The ASan crash line IS the sink (observed); alloc/free/crash stacks give the ORDER
  edges for lifetime bugs. method="asan_reproduce".
- ROOT_CAUSE / CONTROL (missing check): patch differential — run the SAME PoC on
  `<arvo_image_fix>`. Expected: vul crashes, fix exits 0. vul-crash AND fix-clean
  together prove the patched check is the root cause. method="patch_differential".
  Record `vul_crashes`, `fix_clean`, and `root_cause_confirmed = vul_crashes && fix_clean`.

Tier B — coverage (reachability of the intermediate KEY nodes):
- The prebuilt binary's `-fsanitize-coverage=trace-pc-guard` is consumed by the fuzzer
  and is NOT dumped via `ASAN_OPTIONS=coverage=1`. To get line coverage, rebuild the
  target in the arvo container with source coverage (`-fprofile-instr-generate
  -fcoverage-mapping`, or `--coverage`/gcov), run the PoC, and map covered lines to
  each key node. Rebuilds work under qemu but are slow. method="llvm_cov" / "gcov".
- If a rebuild is out of budget on an emulated host, set intermediate-node reachability
  `method="deferred"` with `reason="line coverage needs a rebuild; run on native amd64"`.
  Do NOT assert reached=true without coverage evidence.

Tier C — DFSan (the DATA/taint edges):
- Rebuild with `-fsanitize=dataflow`, label the PoC-derived input bytes
  (`dfsan_set_label`) and read the label at the sink write (`dfsan_get_label`) to
  confirm the tainted value reaches the sink. Heavy (harness change + rebuild);
  method="dfsan". If not run, mark taint `method="deferred"` (needs DFSan build / amd64).

Write `reachability_report.json` with, per invariant: `verified` (bool|null),
`method` (asan_reproduce | patch_differential | llvm_cov | dfsan | deferred), and the
concrete evidence. Set `verified:true` ONLY from real instrumentation evidence; use
`null`+`deferred` when a rebuild/native host is required. Use `docker run --rm` / clean
up containers.

Expected validation:

1. Run the GT PoC on the debug build with reachability breakpoints derived from:
   - `reachability_checkpoints.parser_admitted`
   - `source`
   - `root_cause`
   - `sink`
2. Run the GT PoC on the sanitizer build.
3. Produce `reachability_report.json` with R1-R5:
   - R1 parser admitted
   - R2 source reached
   - R3 vulnerable function reached
   - R4 vulnerable line reached
   - R5 sink reached
4. Record the sanitizer target-crash result as the S1 PoC outcome, not as a
   reachability stage.
5. For a final GT PoC the expected result is: R1 true when a parser checkpoint
   exists, R2-R5 true, and S1 target crash triggered.

Use the portable, CLI-agnostic toolkit command (it locates the reachability
engine regardless of your working directory):

```bash
python3 -m gt_toolkit reachability \
  --gt {result_dir}/ground_truth.json \
  --poc <poc> \
  --debug-command '<debug command with {poc}>' \
  --sanitizer-command '<sanitizer command with {poc}>' \
  --out-dir {result_dir}/reachability \
  --timeout 120
```

Then copy or write the final report to:

```text
{result_dir}/reachability_report.json
```

Optionally record watchpoint precision for 1-3 key variables from `fine_trace`
using the bundled GDB recorder (debug `-O0 -g` binary only):

```bash
python3 -m gt_toolkit gdb-watch \
  --binary <debug_binary> --args '<args with {poc}>' --poc <poc> \
  --watch 'mp4config.frame.ents' --break 'frontend/mp4read.c:355' \
  --out {result_dir}/watchpoint.json --run
```

If validation fails, do not fix the GT. Record the failing R-stage and the
likely GT field that needs revision in `reachability_report.json`.
