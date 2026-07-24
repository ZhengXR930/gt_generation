DESCRIPTION
87...@developer.gserviceaccount.com created issue #1
Mar 10, 2025 10:05PM
Detailed Report: https://oss-fuzz.com/testcase?key=6609330527076352

Project: upx
Fuzzing Engine: afl
Fuzz Target: decompress_packed_file_fuzzer
Job Type: afl_asan_upx
Platform Id: linux

Crash Type: Heap-buffer-overflow READ 8
Crash Address: 0x51b00000229f
Crash State:
  N_BELE_RTP::BEPolicy::get64
  PackLinuxElf64::PackLinuxElf64help1
  PackLinuxElf64ppc::PackLinuxElf64ppc
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=afl_asan_upx&range=202402140608:202411280613

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=6609330527076352

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.