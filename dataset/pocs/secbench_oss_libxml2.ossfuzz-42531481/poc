DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Dec 26, 2023 07:45AM
Detailed Report: https://oss-fuzz.com/testcase?key=5663442862145536

Project: libxml2
Fuzzing Engine: libFuzzer
Fuzz Target: xml
Job Type: libfuzzer_asan_libxml2
Platform Id: linux

Crash Type: Heap-use-after-free READ 8
Crash Address: 0x604000000aa8
Crash State:
  xmlStaticCopyNode
  xmlDocCopyNode
  xmlParseReference
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_libxml2&range=202312250625:202312260612

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5663442862145536

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.