DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Nov 30, 2020 02:42PM
Detailed Report: https://oss-fuzz.com/testcase?key=5758829488504832

Project: php
Fuzzing Engine: honggfuzz
Fuzz Target: php-fuzz-parser
Job Type: honggfuzz_asan_php
Platform Id: linux

Crash Type: Heap-buffer-overflow READ 4
Crash Address: 0x603000000e18
Crash State:
  _zend_is_inconsistent
  zend_hash_find
  zend_hash_find_ptr
 
Sanitizer: address (ASAN)

Recommended Security Severity: Medium

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_php&range=202009020617:202009030625

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=5758829488504832

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.