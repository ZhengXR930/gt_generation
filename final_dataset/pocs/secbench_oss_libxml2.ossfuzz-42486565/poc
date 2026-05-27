DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Aug 14, 2020 10:43PM
Detailed Report: https://oss-fuzz.com/testcase?key=6261568594247680

Project: libxml2
Fuzzing Engine: libFuzzer
Fuzz Target: xml
Job Type: libfuzzer_asan_libxml2
Platform Id: linux

Crash Type: Heap-use-after-free READ 4
Crash Address: 0x60c000000a08
Crash State:
  xmlXIncludeIncludeNode
  xmlXIncludeDoProcess
  xmlXIncludeLoadFallback
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_libxml2&range=202008080619:202008090607

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6261568594247680

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.