DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Sep 12, 2021 09:30AM
Detailed Report: https://oss-fuzz.com/testcase?key=5500806351159296

Project: php
Fuzzing Engine: honggfuzz
Fuzz Target: php-fuzz-execute
Job Type: honggfuzz_asan_php
Platform Id: linux

Crash Type: Heap-use-after-free READ 4
Crash Address: 0x612000016b50
Crash State:
  ZEND_FE_FETCH_RW_SPEC_VAR_HANDLER
  fuzzer_execute_ex
  zend_execute
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_php&range=202008280605:202008290628

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5500806351159296

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.