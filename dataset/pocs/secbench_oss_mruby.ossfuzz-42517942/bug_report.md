DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Nov 9, 2022 08:51AM
Detailed Report: https://oss-fuzz.com/testcase?key=6279993986777088

Project: mruby
Fuzzing Engine: libFuzzer
Fuzz Target: mruby_fuzzer
Job Type: libfuzzer_ubsan_mruby
Platform Id: linux

Crash Type: Index-out-of-bounds
Crash Address:
Crash State:
  str_init_embed
  str_new
  mrb_str_new
 
Sanitizer: undefined (UBSAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_ubsan_mruby&range=202211070614:202211080612

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6279993986777088

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.