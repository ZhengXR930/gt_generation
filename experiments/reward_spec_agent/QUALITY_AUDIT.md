# Reward-Spec Agent quality audit

> Pilot-only evidence: the prompt used for these five runs explicitly allowed
> build/fuzz-harness files present in the source tree. Although no GT was shown,
> this input policy is broader than the final same-input protocol and these
> runs must not be used in the final schema study or effectiveness comparison.

The Reward-Spec Agent saw only each public issue and vulnerable source tree.
Ground truth was consulted only after generation for this audit.

## Results

| Sample | Public issue | Format/admission | Root predicate | Propagation | Target |
|---|---|---|---|---|---|
| `arvo_17855` | Rich causal description | Good: source is exact; the second anchor is on the issue-relevant MMR path | Good at the verified-invariant level: `consumed_bytes > size` at the second-plane call is the violated obligation. It does not name the earlier bookkeeping defect, but it is executable and causally meaningful. | Good partial causal chain: MMR consume accounting reaches returned `consumed_bytes`, then the second-plane underflow | Wrong guess: generated ASan heap overflow; saved GT is MSan use-of-uninitialized-value. The public issue does not state the detector. |
| `arvo_20320` | OSS-Fuzz crash stack only | Reasonable coarse entry anchors | Wrong: `d == 0` is a condition that dispatches toward the crashing traversal, not the lifetime bug. GT root is the look-behind reset that frees a still-aliased node. | Wrong kind of path: mostly a call stack and omits parse, alias binding, reset/free, and stale-alias propagation | Correct because the issue states ASan heap-use-after-free |
| `arvo_15178` | OSS-Fuzz crash stack only | First anchor matches GT source; second is near parser/optimizer admission | Wrong: speculative allocation-field consistency at the outer `bpf_optimize` handler. GT root is the `opt_init` allocation-failure path that frees `edges` and longjmps without clearing it. | Contains real calls but not the dangling-pointer lifetime chain | Correct because the issue states ASan heap-double-free |
| `arvo_16457` | One-sentence out-of-bounds-read description | Weak: starts at a harness callback and jumps deep into residual decoding | Wrong: `cwords == words` in Rice decoding is not the CRC cursor underflow. GT root is `consumed_words <= crc16_offset` at `crc16_update_block_`. | A plausible decode call path, but misses reset/clear/stale-CRC-state propagation | Wrong guess: ASan heap-buffer-overflow; saved GT is an MSan-detected SEGV/read. The issue does not state the detector. |
| `arvo_31301` | Short but causally specific zero-length-hash description | Second anchor reaches font handling, but the first is a generic fuzz-harness entry rather than a project parser admission | Exact: `len == 0` at `fnv_32a_buf` is the missing-guard condition in GT | Superficially plausible call path, but omits the crucial `parse_tags -> update_font` transformation that turns `@` into a zero-length family | Conservative but unusably vague (`sanitizer:crash`); the public issue does not state the ASan oracle |

## Aggregate interpretation

- All 5 outputs are schema-valid and their listed source locations exist.
- Root quality is strong only when the issue itself states a source-groundable
  causal condition: 2/5 are useful (`17855`, `31301`). Crash-stack-only and
  sparse issues produce sink/root confusion or unsupported hypotheses.
- Exact target generation is 2/5, both cases where the public issue explicitly
  names the sanitizer and failure kind. Guessing an unstated detector is unsafe.
- Only 1/5 propagation lists is a useful causal state chain. The other four are
  primarily call/control paths and would reward plausible but irrelevant
  execution.

## Design implication

Do not freeze a hypothesized `propagation` path into the task Reward Spec. Keep
task-specific, executable admission/root/oracle predicates, and calculate
progress from each submitted candidate's verified runtime observations. A
candidate receives progress only when its maximum verified stage exceeds the
best stage previously reached for that task. Candidate-local trace information
can still be returned as diagnostic feedback, but it should not be treated as
ground truth generated from the issue.

The minimal scientific object is therefore closer to:

```json
{
  "admission": ["path:function:line", "path:function:line"],
  "root": {"at": "path:function:line", "when": "boolean expression"},
  "oracle": "sanitizer:failure-kind-or-unknown"
}
```

Global events such as process start, payload consumption, clean exit, timeout,
and submission count belong to the runtime harness rather than the per-task
Reward Spec.
