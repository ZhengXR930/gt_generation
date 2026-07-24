DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
May 24, 2023 08:45AM
Detailed Report: https://oss-fuzz.com/testcase?key=4927979927764992

Project: openexr
Fuzzing Engine: afl
Fuzz Target: openexr_exrcorecheck_fuzzer
Job Type: afl_asan_openexr
Platform Id: linux

Crash Type: Heap-buffer-overflow READ 2
Crash Address: 0x60b000000158
Crash State:
  libdeflate_zlib_decompress_ex
  exr_uncompress_buffer
  DwaCompressor_uncompress
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=afl_asan_openexr&range=202305160611:202305170614

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4927979927764992

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.