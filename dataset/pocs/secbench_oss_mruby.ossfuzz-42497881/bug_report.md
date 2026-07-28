DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jun 9, 2021 12:32PM
Detailed Report: https://oss-fuzz.com/testcase?key=6256907538006016

Project: mruby
Fuzzing Engine: honggfuzz
Fuzz Target: mruby_fuzzer
Job Type: honggfuzz_asan_mruby
Platform Id: linux

Crash Type: Negative-size-param
Crash Address:
Crash State:
  mrb_str_format
  mrb_f_sprintf
  mrb_vm_exec
 
Sanitizer: address (ASAN)

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_mruby&range=202106080618:202106090626

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6256907538006016

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.