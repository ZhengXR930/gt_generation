DESCRIPTION
mo...@clusterfuzz-external.iam.gserviceaccount.com created issue #1
Aug 27, 2021 02:57AM
Detailed Report: https://oss-fuzz.com/testcase?key=4836671271600128

Project: php
Fuzzing Engine: honggfuzz
Fuzz Target: php-fuzz-execute
Job Type: honggfuzz_asan_php
Platform Id: linux

Crash Type: Heap-use-after-free READ 4
Crash Address: 0xf452c5c4
Crash State:
  zend_string_release
  zend_closure_free_storage
  zend_objects_store_del
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=honggfuzz_asan_php&range=202104150634:202104160611

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4836671271600128

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.

This bug is subject to a 90 day disclosure deadline. If 90 days elapse without an upstream patch, then the bug report will automatically become visible to the public.