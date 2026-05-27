DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
May 22, 2023 02:27AM
Detailed Report: https://oss-fuzz.com/testcase?key=5200395937644544

Project: mupdf
Fuzzing Engine: libFuzzer
Fuzz Target: pdf_fuzzer
Job Type: libfuzzer_asan_mupdf
Platform Id: linux

Crash Type: Heap-use-after-free WRITE 1
Crash Address: 0x615000000e50
Crash State:
  pdf_cache_object
  pdf_resolve_indirect
  pdf_resolve_indirect_chain
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_mupdf&range=202305040621:202305050623

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5200395937644544

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.