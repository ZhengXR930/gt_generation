DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jan 22, 2024 02:03PM
Detailed Report: https://oss-fuzz.com/testcase?key=5326425628409856

Project: upx
Fuzzing Engine: libFuzzer
Fuzz Target: list_packed_file_fuzzer
Job Type: libfuzzer_asan_upx
Platform Id: linux

Crash Type: Heap-buffer-overflow READ 1
Crash Address: 0x622000004690
Crash State:
  PackLinuxElf64::elf_lookup
  PackLinuxElf64::PackLinuxElf64help1
  PackLinuxElf64amd::PackLinuxElf64amd
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Crash Revision: https://oss-fuzz.com/revisions?job=libfuzzer_asan_upx&revision=202401220612

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5326425628409856

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.