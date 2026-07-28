DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Sep 9, 2022 07:33AM
Detailed Report: https://oss-fuzz.com/testcase?key=6327216014753792

Project: php
Fuzzing Engine: libFuzzer
Fuzz Target: php-fuzz-function-jit
Job Type: libfuzzer_asan_php
Platform Id: linux

Crash Type: Heap-use-after-free READ 4
Crash Address: 0x60300001b5f4
Crash State:
  timelib_error_container_dtor
  zm_post_zend_deactivate_date
  zend_post_deactivate_modules
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_php&range=202209080614:202209090608

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6327216014753792

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.