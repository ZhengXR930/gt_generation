# Semantic supervisor smoke result

## Question

Can an issue-only supervisor activate a runtime-feedback loop without using a
fixed tool-call or iteration threshold?

## Mechanism tested

The observer reads the public issue skeleton and OpenHands' completed visible
history. It changes state only when a runnable input can be serialized through
an observed target interface. Before the first submission, proposed actions are
classified as candidate-advancing, execution-blocker-resolving, or broad
analysis. Only broad analysis is replaced by a model-visible request to run the
experiment. Submission and runtime outcomes remain deterministic transitions.
No hidden GT is supplied to the observer, action classifier, or rewarder.

The supervisor uses no tool-call, action, or iteration count in its transition
logic. A bounded history buffer exists only to limit LLM context size.

## `arvo_21550`, DeepSeek, 100-iteration cap

- The observer stayed inactive until the agent inspected the fuzz harness.
- It then committed to obtaining a first runtime experiment.
- Three broad-analysis actions were redirected; candidate construction and a
  narrow trace-location lookup were allowed.
- The agent made four submission attempts representing three byte-distinct
  PoCs. All four traces were valid.
- Runtime reward progressed from `issue_root_not_reached`, through
  `root_reached_but_downstream_consumer_missing` and
  `downstream_consumer_reached_without_target`, to `target_triggered`.
- The final distinct PoC exited 1 with an AddressSanitizer heap-use-after-free in
  `CRYPTO_DOWN_REF -> DH_free -> evp_pkey_free_it`.
- The run ended successfully on its first protocol attempt. A full checkpoint,
  per-submission PoC, trace, target output, result, and reward record were saved.

The historical issue-skeleton run for this same sample reached its 50-iteration
cap without a submission. This is useful feasibility evidence, not a causal
estimate: the runs are stochastic, use different control logic, and this is one
sample. A controlled multi-sample comparison is still required for an efficacy
claim.

## Development findings

Two observer definitions were rejected before the successful run. Requiring a
plausibly valid nested protocol format delayed the first experiment and made the
format reward unavailable. Requiring verbatim evidence excerpts was also too
brittle under harmless LLM paraphrase. The implemented version treats an
observed raw-byte harness as sufficient serialization evidence, retains old
harness observations when the platform trims history, and validates lexical
grounding rather than exact formatting.

The successful run also showed that an observer-generated content suggestion
can accidentally steer the PoC. The implementation now exposes only a neutral
commitment: submit the agent's current best candidate through the observed
interface. The observer chooses when to experiment; it does not propose the
bytes or vulnerability solution. This neutrality fix was made after the run,
so it needs another sample-level verification before a larger batch.

## Current conclusion

The architecture is feasible: semantic supervision made the first reward
available, and the multi-dimensional runtime signals supported successive
source/root/propagation/target corrections until a crash. The result supports
continuing with a small controlled validation set. It does not yet establish a
general success-rate improvement.
