# Invalid GT residues

These packages are intentionally outside the active `gt_results/<sample_id>` namespace. They are retained for debugging and possible reruns, but must not be counted as complete GT packages.

## Stage 04 proof failures

| sample | generation status | failed stages | assertion status | reason |
| --- | --- | --- | --- | --- |
| `arvo_10140` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |
| `arvo_12424` | `failed` | 04_assertion_execute | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |
| `arvo_19208` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |
| `arvo_25378` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |
| `arvo_42470156` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | no assertions were verified |
| `arvo_42506975` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | no assertions were verified |
| `arvo_42537883` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |
| `osv_ossfuzz_OSV-2025-148` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |
| `osv_ossfuzz_OSV-2026-74` | `failed` | 04_assertion_execute | `all_verified=None, required_verified=None` | no assertions were verified |
| `secbench_cve_gpac.cve-2023-48014` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |
| `secbench_oss_libxml2.ossfuzz-42487785` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |
| `secbench_oss_libxml2.ossfuzz-42517254` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |
| `secbench_oss_libxml2.ossfuzz-42528997` | `failed` | 04_assertion_validator | `all_verified=False, required_verified=None` | required root-obligation assertion was not verified |

## Package audit failures

| sample | category | first audit error |
| --- | --- | --- |
| `arvo_10841` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'fuzz/librawspeed/fuzz/Common.cpp' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_12959` | `harness_anchor` | ground_truth: sink is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_16081` | `reachability_gate` | reachability artifact breakpoints is missing: reachability/reachability_breakpoints.json |
| `arvo_16820` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'fuzz/jpegsave_file_fuzzer.cc' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_18140` | `harness_anchor` | ground_truth: root_cause is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_22110` | `harness_anchor` | ground_truth: root_cause is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_23717` | `harness_anchor` | ground_truth: root_cause is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_30099` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'tests/fuzz/fuzz.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_34299` | `reachability_gate` | reachability artifact breakpoints is missing: reachability/reachability_breakpoints.json |
| `arvo_37056` | `reachability_gate` | reachability_report.json does not satisfy GT generation gate (R1/R2/R3/R5 plus sink line or verified sink assertion event) |
| `arvo_38359` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'fuzzer/fuzzer.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_42275` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'fuzzers/tint_common_fuzzer.cc' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_42483053` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'test/fuzzing/named_arg.cpp' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_42483087` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'test/fuzzing/named_arg.cpp' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_42487630` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'test/fuzzing/fuzzer_common.h' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_42490732` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'curl_fuzzer/curl_fuzzer.cc' looks like a fuzzing harness source; use the project source statement for this vulnerability anchor |
| `arvo_42501995` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_42502820` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'curl_fuzzer/curl_fuzzer_tlv.cc' looks like a fuzzing harness source; use the project source statement for this vulnerability anchor |
| `arvo_42503442` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'src/lib/util/fuzzer.c' looks like a fuzzing harness source; use the project source statement for this vulnerability anchor |
| `arvo_42503978` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'src/lib/util/fuzzer.c' looks like a fuzzing harness source; use the project source statement for this vulnerability anchor |
| `arvo_42508600` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'test/fuzzing/one-arg.cc' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_42508631` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'test/fuzzing/chrono-duration.cc' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_42534620` | `harness_anchor` | ground_truth: root_cause is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_42534993` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'fuzz/ssl_ctx_api.cc' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_42541538` | `harness_anchor` | ground_truth: root_cause is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_47143` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'src/tests/fuzzing/fuzzer_tool.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_48884` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'ossfuzz/fuzz_data_producer.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_48910` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'ossfuzz/round_trip_frame_uncompressed_fuzzer.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_48993` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'ossfuzz/round_trip_frame_uncompressed_fuzzer.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_48997` | `harness_anchor` | ground_truth: root_cause is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_53046` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'harnesses/base.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_53499` | `harness_anchor` | ground_truth: root_cause is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_57429` | `harness_anchor` | ground_truth: root_cause is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_58295` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'Modules/_xxtestfuzz/fuzzer.c' looks like a fuzzing harness source; use the project source statement for this vulnerability anchor |
| `arvo_63314` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'fuzzer/ultrahdr_enc_fuzzer.cpp' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_66063` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: function 'LLVMFuzzerTestOneInput' is a fuzzing harness entry point; use the project source statement for this vulnerability anchor |
| `arvo_6626` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'src/fuzzer/invert.cpp' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `arvo_66510` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'tests/fuzz/fuzz_data_producer.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `osv_ossfuzz_OSV-2026-608` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'fuzzing/FuzzStunClient.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `secbench_oss_libxml2.ossfuzz-42486565` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'fuzz/fuzz.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
| `secbench_oss_libxml2.ossfuzz-42486665` | `harness_anchor` | ground_truth: source is anchored in unscored fuzzing harness code: file 'fuzz/fuzz.c' is under a fuzzing harness directory; use the project source statement for this vulnerability anchor |
