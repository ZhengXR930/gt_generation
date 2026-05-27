DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jan 16, 2021 12:22PM
Detailed Report: https://oss-fuzz.com/testcase?key=4922677364129792

Project: php
Fuzzing Engine: libFuzzer
Fuzz Target: php-fuzz-unserializehash
Job Type: libfuzzer_msan_php
Platform Id: linux

Crash Type: UNKNOWN READ
Crash Address: 0x70210000c354
Crash State:
  XXH_memcpy
  XXH_INLINE_XXH32_update
  PHP_XXH32Update
 
Sanitizer: memory (MSAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_msan_php&range=202101090626:202101120620

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4922677364129792

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.