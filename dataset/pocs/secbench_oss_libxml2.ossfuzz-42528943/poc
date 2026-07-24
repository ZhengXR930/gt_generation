DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Oct 7, 2023 12:30PM
Detailed Report: https://oss-fuzz.com/testcase?key=4817272335892480

Project: libxslt
Fuzzing Engine: libFuzzer
Fuzz Target: xslt
Job Type: libfuzzer_msan_libxslt
Platform Id: linux

Crash Type: Use-of-uninitialized-value
Crash Address:
Crash State:
  xmlStrdup
  __xmlRaiseError
  xmlFatalErrMsgStr
 
Sanitizer: memory (MSAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_msan_libxslt&range=202310060600:202310070604

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4817272335892480

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.