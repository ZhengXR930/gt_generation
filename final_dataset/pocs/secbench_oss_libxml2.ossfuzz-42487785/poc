DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Sep 27, 2020 05:24PM
Detailed Report: https://oss-fuzz.com/testcase?key=6039216293937152

Project: libxml2
Fuzzing Engine: honggfuzz
Fuzz Target: xml
Job Type: honggfuzz_asan_libxml2
Platform Id: linux

Crash Type: Heap-use-after-free READ 4
Crash Address: 0x60c000002208
Crash State:
  xmlStaticCopyNode
  xmlCopyNode
  xmlXIncludeLoadTxt
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_libxml2&range=202008250617:202008260620

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6039216293937152

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.