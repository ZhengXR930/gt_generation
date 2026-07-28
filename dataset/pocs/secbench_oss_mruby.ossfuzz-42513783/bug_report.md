DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jul 24, 2022 06:13AM
Detailed Report: https://oss-fuzz.com/testcase?key=6172524181848064

Project: mruby
Fuzzing Engine: libFuzzer
Fuzz Target: mruby_proto_fuzzer
Job Type: libfuzzer_asan_mruby
Platform Id: linux

Crash Type: Segv on unknown address
Crash Address:
Crash State:
  urshift
  mpz_div_2exp
  mrb_bint_rshift
 
Sanitizer: address (ASAN)

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_mruby&range=202207230610:202207240606

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6172524181848064

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.