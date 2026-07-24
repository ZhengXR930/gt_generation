DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
May 3, 2022 08:02AM
Detailed Report: https://oss-fuzz.com/testcase?key=4972643922608128

Project: mruby
Fuzzing Engine: honggfuzz
Fuzz Target: mruby_proto_fuzzer
Job Type: honggfuzz_asan_mruby
Platform Id: linux

Crash Type: Heap-use-after-free READ 8
Crash Address: 0x619000002250
Crash State:
  mrb_funcall_with_block
  mrb_instance_new
  mrb_funcall_with_block
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_mruby&range=202205020603:202205030608

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4972643922608128

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.