DESCRIPTION
87...@developer.gserviceaccount.com created issue #1
Mar 9, 2025 04:13PM
Detailed Report: https://oss-fuzz.com/testcase?key=5934289493753856

Project: mruby
Fuzzing Engine: afl
Fuzz Target: mruby_fuzzer
Job Type: afl_asan_mruby
Platform Id: linux

Crash Type: Heap-buffer-overflow WRITE 8
Crash Address: 0x52f00000c420
Crash State:
  range_num_to_a
  mrb_vm_exec
  mrb_funcall_with_block
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=afl_asan_mruby&range=202302160607:202302170603

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5934289493753856

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.