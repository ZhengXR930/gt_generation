DESCRIPTION
87...@developer.gserviceaccount.com created issue #1
May 12, 2025 01:44PM
Detailed Report: https://oss-fuzz.com/testcase?key=5866431243943936

Project: libxml2
Fuzzing Engine: libFuzzer
Fuzz Target: valid
Job Type: libfuzzer_asan_libxml2
Platform Id: linux

Crash Type: Stack-use-after-scope READ 8
Crash Address: 0x7b60a58022a0
Crash State:
  xmlEscapeText
  xmlNodeListGetStringInternal
  xmlValidateElement
 
Sanitizer: address (ASAN)

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_libxml2&range=202505110645:202505120646

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5866431243943936

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.