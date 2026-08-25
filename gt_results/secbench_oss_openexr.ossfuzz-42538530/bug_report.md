DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Sep 17, 2024 02:37PM
Detailed Report: https://oss-fuzz.com/testcase?key=5191937005518848

Project: openexr
Fuzzing Engine: libFuzzer
Fuzz Target: openexr_exrcorecheck_fuzzer
Job Type: libfuzzer_asan_openexr
Platform Id: linux

Crash Type: UNKNOWN READ
Crash Address: 0x00009f046110
Crash State:
  MemcmpInterceptorCommon
  internal_exr_validate_shared_attrs
  internal_exr_parse_header
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_openexr&range=202409140619:202409150641

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5191937005518848

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on issues here are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.