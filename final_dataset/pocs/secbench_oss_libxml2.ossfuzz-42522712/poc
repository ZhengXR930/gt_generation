DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Mar 26, 2023 03:17PM
Detailed Report: https://oss-fuzz.com/testcase?key=4953329095802880

Project: libxml2
Fuzzing Engine: libFuzzer
Fuzz Target: html
Job Type: libfuzzer_asan_libxml2
Platform Id: linux

Crash Type: Global-buffer-overflow READ 1
Crash Address: 0x0000008f81f3
Crash State:
  xmlStrndup
  htmlParseSystemLiteral
  htmlParseDocTypeDecl
 
Sanitizer: address (ASAN)

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_libxml2&range=202303230606:202303240620

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4953329095802880

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.