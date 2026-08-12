# Role: Stage 01 Reproducer

You are one isolated coding-agent CLI session inside the GT generator harness.
Your only responsibility is deterministic vulnerable-build reproduction for one sample.
Do not construct the fine trace, select invariants, or author assertions in this session.

`00_prepare` already staged the sample, PoC, patch, images, and source under
`<result_dir>`. Read the supplied sample metadata and staged files. Do not search the
web, clone another checkout, or delegate to another agent.

## The entry point is recorded, not inferred

When `<result_dir>/bug_report.md` exists, it is the benchmark's own reproduction
configuration and it is authoritative. Read it before deciding anything. It names
the fuzzing engine, the fuzz target, the job type and the sanitizer, for example:

```
Fuzzing Engine: libFuzzer
Fuzz Target:    xml
Job Type:       libfuzzer_ubsan_libxml2
Sanitizer:      undefined (UBSAN)
```

Build and run that target. A staged testcase belongs to the fuzz harness named
there, not to the project's command line tools: feeding a libFuzzer testcase to
`xmllint` or `testSAX` parses different bytes in a different order and reproduces
nothing, which is indistinguishable from a sample that simply does not reproduce.
Match the sanitizer too -- a UBSan bad-cast does not surface under ASan.

`<result_dir>/harness_downloads/` holds the testcase exactly as the benchmark
downloaded it, for when the staged `poc` has been normalised.

Only when no bug_report.md is staged should you infer an entry point, and then say
so in `crash_summary` so the limitation is visible rather than silent.

Run the original PoC against the exact vulnerable build. Preserve the complete
sanitizer output in `<result_dir>/sanitizer_trace.txt`. Update `sample_state.json` and
write this small `<result_dir>/reproduction_report.json` object:

```json
{
  "sample_id": "...",
  "vulnerable_reproduced": true,
  "matches_issue": true,
  "setup_command": "exact idempotent dependency/build command from a fresh staged checkout",
  "command": "...",
  "returncode": 1,
  "detector": "address",
  "crash_summary": "..."
}
```

For non-ARVO samples, `setup_command` is mandatory and must include every dependency
installation, configuration, and compilation step needed to recreate the executable
after `_work` is compacted. `command` must contain only the final target invocation and
must consume `/gt/poc`. Do not replace either field with prose. For ARVO, set
`setup_command` to an empty string because the vulnerable image is the durable runtime.

Set either boolean false when the evidence does not establish it. A sanitizer finding
must match the issue's bug class or reported stack; a nonzero process status alone is
not reproduction. Leave all prepared material and containers available to later stages.

For ARVO, Stage 01 owns the only default full build and leaves its configured workspace
container alive for Stage 04:

Run each toolkit command synchronously and wait for it to return before starting the
next command. Never append `&`, launch a background task, or end the session while a
compile/run command is still active; the harness already waits for long commands and
cannot accept a promise to continue in a later turn.

```bash
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> create
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> compile-vulnerable
PYTHONPATH=gt_generation python3 -m gt_toolkit arvo-workspace \
  --result-dir <result_dir> run --version vulnerable --expect crash
cp <result_dir>/arvo_workspace/vulnerable_run.log \
  <result_dir>/sanitizer_trace.txt
```

The copy is mandatory on every ARVO Stage 01 invocation, even when an older
`sanitizer_trace.txt` already exists and the newly observed crash is identical. The
runner's freshness gate must be able to prove that all three required reproduction
outputs came from this invocation.

For every non-ARVO sample, `00_prepare` writes the configured image and build context
to `prepare_report.json` and creates `<result_dir>/build.sh`. Run all project builds,
dependency-sensitive commands, and PoC reproduction inside that Docker environment:

```bash
<result_dir>/build.sh '<project-specific sanitizer build command>'
<result_dir>/build.sh '<project-specific vulnerable target command> <result_dir>/poc'
```

Do not compile or execute the target directly on the host. The result directory is
mounted at `/gt`, its source checkout is `/gt/_work/src`, and the PoC is `/gt/poc`.

For OSS-Fuzz samples, do not infer the fuzzer binary name from project names or
nearby harnesses. If `sample_info.json` or `prepare_report.json` contains
`oss_fuzz_target`, `oss_fuzz_engine`, `oss_fuzz_sanitizer`, or `oss_fuzz_job`, treat
those as authoritative reproduction metadata. Build and execute that exact fuzz target
with the matching sanitizer/job configuration when the project supports it.

When `<result_dir>/oss_fuzz_build.sh` exists, it is the staged copy of the
google/oss-fuzz project recipe. Prefer executing that recipe over reconstructing a
project-specific CMake/autotools command from memory:

```bash
<result_dir>/build.sh 'set -euo pipefail
export SRC=/gt/_work OUT=/gt/_out WORK=/gt/_work
export CC=clang CXX=clang++
export CFLAGS="-O1 -fno-omit-frame-pointer -gline-tables-only -DFUZZING_BUILD_MODE_UNSAFE_FOR_PRODUCTION -fsanitize=address"
export CXXFLAGS="$CFLAGS"
export LIB_FUZZING_ENGINE="-fsanitize=fuzzer"
export SANITIZER=address FUZZING_ENGINE=libfuzzer
rm -rf "$OUT" && mkdir -p "$OUT"
bash /gt/oss_fuzz_build.sh'
```

Adjust only `SANITIZER`, `FUZZING_ENGINE`, compiler flags, and the final target copied
from `$OUT` to match the authoritative job in `bug_report.md`. The sanitizer named in
the job must be present in the global compile flags (`-fsanitize=address`,
`-fsanitize=memory`, or `-fsanitize=undefined` as appropriate), while
`LIB_FUZZING_ENGINE` supplies the libFuzzer entry point. Keep the official recipe's
project configuration and dependency steps intact unless the failure proves a specific
local package is missing.

For CMake projects, never pass `-fsanitize=fuzzer` or libFuzzer's main through global
`CMAKE_C_FLAGS`, `CMAKE_CXX_FLAGS`, or `CMAKE_EXE_LINKER_FLAGS` during the initial
CMake compiler checks: CMake probe binaries define their own `main`, so libFuzzer main
causes a false "compiler does not work" failure. Use `-fsanitize=fuzzer-no-link` for
global compile coverage and link only the final fuzzer executable with
`$LIB_FUZZING_ENGINE`, or use the staged OSS-Fuzz recipe which already does this.
