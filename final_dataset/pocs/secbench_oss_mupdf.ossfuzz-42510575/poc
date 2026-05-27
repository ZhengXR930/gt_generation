DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Apr 9, 2022 12:40PM
Detailed Report: https://oss-fuzz.com/testcase?key=4651158246916096

Project: mupdf
Fuzzing Engine: libFuzzer
Fuzz Target: pdf_fuzzer
Job Type: libfuzzer_msan_mupdf
Platform Id: linux

Crash Type: Use-of-uninitialized-value
Crash Address:
Crash State:
  fz_drop_buffer
  pdf_load_embedded_font
  pdf_load_font_descriptor
 
Sanitizer: memory (MSAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_msan_mupdf&range=202204080600:202204090606

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4651158246916096

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.