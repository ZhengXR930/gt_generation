DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Dec 2, 2023 02:14PM
Detailed Report: https://oss-fuzz.com/testcase?key=6122240017825792

Project: mupdf
Fuzzing Engine: libFuzzer
Fuzz Target: pdf_fuzzer
Job Type: libfuzzer_asan_mupdf
Platform Id: linux

Crash Type: Heap-buffer-overflow READ {*}
Crash Address: 0x61900000099e
Crash State:
  fz_new_buffer_from_copied_data
  bmp_read_color_profile
  bmp_read_image
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_mupdf&range=202312010612:202312020617

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6122240017825792

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.