DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Jan 4, 2024 09:22AM
Detailed Report: https://oss-fuzz.com/testcase?key=5290502832324608

Project: upx
Fuzzing Engine: afl
Fuzz Target: test_packed_file_fuzzer
Job Type: afl_asan_upx
Platform Id: linux

Crash Type: Stack-buffer-overflow READ 4
Crash Address: 0x7ffc75eac2f0
Crash State:
  PackLinuxElf64::unpack
  Packer::doTest
  do_one_file
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Crash Revision: https://oss-fuzz.com/revisions?job=afl_asan_upx&revision=202401040604

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5290502832324608

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.