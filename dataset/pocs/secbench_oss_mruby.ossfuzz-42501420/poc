DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Sep 9, 2021 05:40AM
Detailed Report: https://oss-fuzz.com/testcase?key=5725276449931264

Project: mruby
Fuzzing Engine: honggfuzz
Fuzz Target: mruby_fuzzer
Job Type: honggfuzz_asan_mruby
Platform Id: linux

Crash Type: Heap-buffer-overflow READ 8
Crash Address: 0x62f00000c430
Crash State:
  value_move
  mrb_ary_splice
  mrb_ary_aset
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_mruby&range=202109060612:202109090600

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5725276449931264

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.