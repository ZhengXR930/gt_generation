DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jun 8, 2023 02:24PM
Detailed Report: https://oss-fuzz.com/testcase?key=5172704131809280

Project: libxslt
Fuzzing Engine: honggfuzz
Fuzz Target: xslt
Job Type: honggfuzz_asan_libxslt
Platform Id: linux

Crash Type: Global-buffer-overflow READ {*}
Crash Address: 0x000000828320
Crash State:
  xmlDictLookup
  xmlParseNCName
  xmlParseQName
 
Sanitizer: address (ASAN)

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_libxslt&range=202306070618:202306080601

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5172704131809280

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.