# GT-Free Reward Framework

This package separates task-local runtime reward from cross-sample OpenHands
harness evolution. Ground truth is never materialized in an Agent view and is
not an optimization signal.

## Two time scales

```text
one episode (frozen harness_vN)
issue + vulnerable source -> Reward Spec
Subject trajectory -> submit_candidate -> checkpoint
PoC + trace -> runtime probes -> ordered stage assessment -> factual Reward
trigger success or iteration limit
                         |
                         v
episode-end deterministic metrics + Codex Experience Analyzer
                         |
                         v
append-only cross-sample Experience Pool
                         |
                         v
Codex Harness Patcher -> isolated OpenHands fork -> validate/rollback
                         |
                         v
next process loads harness_vN+1
```

The harness is immutable for every retry belonging to one sample. There is no
in-episode source patch, process restart, or policy hot swap.

## Information boundary

The task-local Reward Agent sees only:

- public issue description;
- vulnerable codebase;
- Subject trajectory and submitted fine trace;
- candidate execution and passive runtime facts;
- earlier evidence from the same episode.

It does not see GT fine traces, invariant graphs, known PoCs, historical crash
states, held-out results, patches, or sanitizer traces supplied by the dataset.

The cross-sample Harness Patcher sees even less task content:

- a complete isolated OpenHands source fork;
- generalized episode metrics;
- enumerated experience categories and local trajectory sequence references;
- prior harness version and Patch history.

It does not receive task ids, issue text, vulnerable source, PoCs, candidate
traces, runtime prose, or GT. Source validation also rejects dataset-specific
literals in a proposed OpenHands Patch.

## Task-local state

Each episode state directory contains:

- `task_context.json`: immutable issue, source manifest, and Reward Spec;
- `trajectory_state.json`: complete platform-neutral trajectory;
- `evidence_state.json`: attempts, runtime facts, causal progress, and errors;
- `harness_state.json`: the frozen OpenHands fork version used by the episode;
- `candidates/`: content-deduplicated PoCs and every submission/checkpoint;
- `evidence/`: immutable runtime/assessment/feedback record per attempt;
- `episode_experience.json`: GT-free end-of-episode experience card;
- `cross_sample_update.json`: pool append and next harness version result.

## Reward Agent

One persistent read-only Codex CLI session is resumed across four task-local
roles:

1. compile `Admission -> Source -> Root -> Propagation -> Target` from public
   issue plus source;
2. observe the full trajectory and request submission at semantic readiness;
3. align untrusted trace claims with source-valid passive probes;
4. turn trusted runtime evidence into factual, non-prescriptive feedback.

Stage status remains deterministic and controller-owned. Absence of evidence is
`unresolved`, not `refuted`; later observations behind a failed causal gate are
`observed_but_blocked`.

## Experience Pool and multi-objective optimization

Every completed episode contributes exact metrics for:

- trigger success;
- no-submission behavior and first-submission sequence;
- total, unique, duplicate, and invalid submissions;
- Reward events followed by Subject action;
- distinct retries after Reward;
- ordered causal progress;
- instrumentation unavailability and premature finish attempts.

The Experience Analyzer may classify only enumerated, evidence-bound categories
such as `missing_submission`, `duplicate_candidate_loop`,
`reward_context_loss`, `productive_retry`, or `causal_stagnation`. The pool
stores successes as controls as well as failures. Raw submission count is never
the sole objective.

## Cross-sample Harness Patcher

A separate persistent workspace-write Codex CLI session runs only after an
episode ends. It may change real files under OpenHands controller, CodeAct,
memory, core loop, and prompt source. The controller checks:

- changed files exactly match the structured declaration;
- the Patch stays inside the allowed OpenHands harness surface;
- Python files parse and files are not deleted;
- no dataset/result/GT literals appear;
- controller-owned Experience Pool input remains unchanged.

Rejected changes restore the complete pre-Patch worktree. Accepted changes are
stored as a unified diff, changed-file snapshot, metadata, and a monotonically
increasing harness version. The next sample receives a frozen copy through
`PYTHONPATH`; pristine baseline evaluation still imports the untouched upstream
checkout.

## Experimental variants

```bash
# untouched formal baseline
python poc_generation/poc_generator/run_sample.py ... --harness-profile baseline

# complete method: Reward plus GT-free cross-sample harness evolution
python poc_generation/poc_generator/run_sample.py ... \
  --harness-profile reward \
  --harness-training-dir /path/to/shared_training_state

# the same complete method in held-out validation/test mode
python poc_generation/poc_generator/run_sample.py ... \
  --harness-profile reward \
  --freeze-harness-updates \
  --harness-training-dir /path/to/frozen_training_state
```

There are only two experimental variants: `baseline` and `reward`. Use
sequential training samples for `reward`: the next sample is the unit that
consumes an accepted harness update. `--freeze-harness-updates` is a lifecycle
control for validation/test, not a third method variant.
