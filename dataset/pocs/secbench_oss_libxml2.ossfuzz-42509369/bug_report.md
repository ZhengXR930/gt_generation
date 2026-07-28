DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Mar 10, 2022 02:34PM
Detailed Report: https://oss-fuzz.com/testcase?key=6139378353700864

Project: libxml2
Fuzzing Engine: libFuzzer
Fuzz Target: schema
Job Type: libfuzzer_asan_i386_libxml2
Platform Id: linux

Crash Type: Invalid-free
Crash Address: 0xf30037c8
Crash State:
  xmlFreeNodeList
  xmlFreeNode
  xmlParseBalancedChunkMemoryInternal
 
Sanitizer: address (ASAN)

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_i386_libxml2&range=202203020602:202203100611

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6139378353700864

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.