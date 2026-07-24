DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jan 5, 2021 04:55AM
Detailed Report: https://oss-fuzz.com/testcase?key=4532420363812864

Project: php
Fuzzing Engine: libFuzzer
Fuzz Target: php-fuzz-unserialize
Job Type: libfuzzer_asan_php
Platform Id: linux

Crash Type: Stack-use-after-return READ 1
Crash Address: 0x7f3a73cf7308
Crash State:
  zval_get_type
  php_var_unserialize_internal
  process_nested_data
 
Sanitizer: address (ASAN)

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_php&range=202101040602:202101050602

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4532420363812864

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.