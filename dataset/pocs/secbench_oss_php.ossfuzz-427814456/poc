DESCRIPTION
87...@developer.gserviceaccount.com created issue #1
Jun 26, 2025 03:22AM
Detailed Report: https://oss-fuzz.com/testcase?key=4692153370738688

Project: php
Fuzzing Engine: libFuzzer
Fuzz Target: php-fuzz-tracing-jit
Job Type: libfuzzer_asan_php
Platform Id: linux

Crash Type: Heap-use-after-free READ 6
Crash Address: 0x5030000157d8
Crash State:
  xbuf_format_converter
  zend_vstrpprintf
  zend_error_va_list
 
Sanitizer: address (ASAN)

Recommended Security Severity: High

Regressed: https://oss-fuzz.com/revisions?job=libfuzzer_asan_php&range=202110110609:202110160600

Reproducer Testcase: https://oss-fuzz.com/download?testcase_id=4692153370738688

Issue filed automatically.

See https://google.github.io/oss-fuzz/advanced-topics/reproducing for instructions to reproduce this bug locally.
When you fix this bug, please
  * mention the fix revision(s).
  * state whether the bug was a short-lived regression or an old bug in any stable releases.
  * add any other useful information.
This information can help downstream consumers.

If you need to contact the OSS-Fuzz team with a question, concern, or any other feedback, please file an issue at https://github.com/google/oss-fuzz/issues. Comments on individual Monorail issues are not monitored.