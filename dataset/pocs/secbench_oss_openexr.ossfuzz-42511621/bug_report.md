DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
May 16, 2022 10:30PM
Detailed Report: https://oss-fuzz.com/testcase?key=5266494809833472

Project: openexr
Fuzzing Engine: afl
Fuzz Target: openexr_exrcorecheck_fuzzer
Job Type: afl_asan_openexr
Platform Id: linux

Crash Type: Heap-buffer-overflow READ 4
Crash Address: 0x6030000001d7
Crash State:
  generic_unpack
  exr_decoding_run
  Imf_3_2::checkCoreFile
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=afl_asan_openexr&range=202205150606:202205160606

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5266494809833472

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.