DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Aug 9, 2023 11:37AM
Detailed Report: https://oss-fuzz.com/testcase?key=5987253640626176

Project: libxml2
Fuzzing Engine: libFuzzer
Fuzz Target: html
Job Type: libfuzzer_asan_i386_libxml2
Platform Id: linux

Crash Type: Heap-use-after-free READ 2
Crash Address: 0xf5000650
Crash State:
  __xmlRaiseError
  htmlParseErr
  htmlCurrentChar
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_i386_libxml2&range=202308080613:202308090608

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5987253640626176

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.