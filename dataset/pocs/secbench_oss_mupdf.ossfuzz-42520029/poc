DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jan 12, 2023 07:59PM
Detailed Report: https://oss-fuzz.com/testcase?key=6321306205552640

Project: mupdf
Fuzzing Engine: libFuzzer
Fuzz Target: pdf_fuzzer
Job Type: libfuzzer_asan_mupdf
Platform Id: linux

Crash Type: Stack-buffer-overflow READ 4
Crash Address: 0x7fbf89fb0c20
Crash State:
  pdf_map_one_to_many
  pdf_parse_bf_range
  pdf_load_cmap
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_mupdf&range=202301110602:202301120618

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6321306205552640

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.