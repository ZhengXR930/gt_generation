DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Oct 19, 2022 04:53PM
Detailed Report: https://oss-fuzz.com/testcase?key=6233968358064128

Project: libxml2
Fuzzing Engine: libFuzzer
Fuzz Target: xml
Job Type: libfuzzer_asan_i386_libxml2
Platform Id: linux

Crash Type: Heap-use-after-free READ 4
Crash Address: 0xf50014c4
Crash State:
  xmlXIncludeCopyXPointer
  xmlXIncludeDoProcess
  xmlXIncludeProcessTreeFlagsData
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_i386_libxml2&range=202210180605:202210190610

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6233968358064128

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.