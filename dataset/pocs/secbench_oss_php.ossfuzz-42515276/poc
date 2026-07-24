DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Aug 30, 2022 09:36AM
Detailed Report: https://oss-fuzz.com/testcase?key=5184455517536256

Project: php
Fuzzing Engine: honggfuzz
Fuzz Target: php-fuzz-execute
Job Type: honggfuzz_asan_php
Platform Id: linux

Crash Type: Heap-use-after-free READ 4
Crash Address: 0x60300001b0b0
Crash State:
  php_date_initialize
  zim_DateTime___construct
  execute_internal
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_php&range=202208290603:202208300612

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5184455517536256

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.