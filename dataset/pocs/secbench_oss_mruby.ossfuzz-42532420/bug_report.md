DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jan 27, 2024 07:37AM
Detailed Report: https://oss-fuzz.com/testcase?key=6237761044545536

Project: mruby
Fuzzing Engine: libFuzzer
Fuzz Target: mruby_fuzzer
Job Type: libfuzzer_asan_mruby
Platform Id: linux

Crash Type: Heap-buffer-overflow READ 8
Crash Address: 0x60300000041a
Crash State:
  mrb_memsearch
  mrb_str_split_m
  mrb_vm_exec
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_mruby&range=202401260603:202401270600

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6237761044545536

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.