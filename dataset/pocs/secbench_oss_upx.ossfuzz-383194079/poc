DESCRIPTION
87...@developer.gserviceaccount.com created issue #1
Dec 10, 2024 03:16AM
Detailed Report: https://oss-fuzz.com/testcase?key=4570665610969088

Project: upx
Fuzzing Engine: libFuzzer
Fuzz Target: test_packed_file_fuzzer
Job Type: libfuzzer_asan_upx
Platform Id: linux

Crash Type: Heap-buffer-overflow READ 4
Crash Address: 0x61f000001a54
Crash State:
  PackMachBase<N_Mach::MachClass_32<N_BELE_CTP::LEPolicy> >::canUnpack
  PackMachFat::canUnpack
  try_can_unpack
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Crash Revision: https://oss-fuzz.com/revisions?job=libfuzzer_asan_upx&revision=202402120613

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4570665610969088

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.