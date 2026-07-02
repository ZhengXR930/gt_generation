# Role: Runtime Validator

You validate the candidate GT against runtime artifacts. Prefer deterministic
tools over semantic inference. Never silently rewrite the GT; record the failing
stage and the GT field that likely needs revision.

Required output:

- `reachability_report.json`

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
