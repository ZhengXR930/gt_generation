DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Sep 18, 2024 10:57AM
Detailed Report: https://oss-fuzz.com/testcase?key=5807710761713664

Project: mruby
Fuzzing Engine: libFuzzer
Fuzz Target: mruby_fuzzer
Job Type: libfuzzer_asan_mruby
Platform Id: linux

Crash Type: Heap-use-after-free READ 8
Crash Address: 0x51b0000006e8
Crash State:
  mrb_vm_exec
  mrb_mod_initialize
  mrb_vm_exec
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_mruby&range=202408050604:202408060601

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5807710761713664

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on issues here are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.