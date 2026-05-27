DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jul 8, 2022 06:08AM
Detailed Report: https://oss-fuzz.com/testcase?key=5309866209574912

Project: mruby
Fuzzing Engine: honggfuzz
Fuzz Target: mruby_fuzzer
Job Type: honggfuzz_asan_mruby
Platform Id: linux

Crash Type: Heap-use-after-free READ 1
Crash Address: 0x60d000000161
Crash State:
  mrb_bint_new_str
  mrb_vm_exec
  mrb_vm_run
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_mruby&range=202207070608:202207080609

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5309866209574912

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.