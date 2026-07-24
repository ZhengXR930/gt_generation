DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Feb 13, 2024 06:29AM
Detailed Report: https://oss-fuzz.com/testcase?key=4618041442762752

Project: mruby
Fuzzing Engine: afl
Fuzz Target: mruby_fuzzer
Job Type: afl_asan_mruby
Platform Id: linux

Crash Type: Heap-buffer-overflow READ 3
Crash Address: 0x6030000003ed
Crash State:
  mrb_memsearch
  str_convert_range
  mrb_str_aref
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=afl_asan_mruby&range=202402090624:202402100609

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4618041442762752

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.