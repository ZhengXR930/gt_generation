DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Mar 18, 2023 09:32AM
Detailed Report: https://oss-fuzz.com/testcase?key=6314169344589824

Project: libxml2
Fuzzing Engine: libFuzzer
Fuzz Target: html
Job Type: libfuzzer_msan_libxml2
Platform Id: linux

Crash Type: Use-of-uninitialized-value
Crash Address:
Crash State:
  htmlParseHTMLAttribute
  htmlParseStartTag
  htmlParseContentInternal
 
Sanitizer: memory (MSAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_msan_libxml2&range=202303150609:202303160615

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6314169344589824

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.