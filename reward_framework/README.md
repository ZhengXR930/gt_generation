# GT-Free Reward Framework

This package separates task-local runtime reward from cross-sample OpenHands
harness evolution. Ground truth is never materialized in an Agent view and is
not an optimization signal.

The method has a task-local assertion protocol and a platform-neutral ordered
control graph. The assertion protocol consists of alternative Admission
locations plus Required safety obligations, Observed unsafe-state predicates,
and optional ordered Transition predicates. The control graph (`Activation ->
Availability -> Consumption -> Progress -> Success`) identifies the first
broken feedback boundary. Episode analysis persists that result as
`stage_control`; the cross-sample Patcher may act only on the earliest
harness-owned blocked stage, never on a downstream symptom or on Subject causal
stagnation by itself.

## Two time scales

```text
one episode (frozen harness_vN)
issue + vulnerable source -> Reward Spec
Subject trajectory -> submit_candidate -> checkpoint
PoC + trace -> assertion probes -> deterministic assessment -> factual Reward
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

## Subject-model recovery

Reward-harness runs bound every Subject-model request and use OpenHands' native
in-place retry loop. Defaults are 90 seconds and three total attempts with
bounded exponential backoff. A transient failure and its later recovery are
persisted as `subject_llm_retryable_error` and `subject_llm_recovered` events.
If every attempt fails, the episode is marked `episode_aborted` and is excluded
from the cross-sample experience pool.

The defaults can be overridden with
`REWARD_FRAMEWORK_SUBJECT_LLM_TIMEOUT`,
`REWARD_FRAMEWORK_SUBJECT_LLM_ATTEMPTS`,
`REWARD_FRAMEWORK_SUBJECT_LLM_RETRY_MIN_WAIT`, and
`REWARD_FRAMEWORK_SUBJECT_LLM_RETRY_MAX_WAIT`. Baseline evaluation does not use
these reward-harness settings.

## Information boundary

The task-local Reward Agent sees only:

- public issue description;
- vulnerable codebase;
- Subject trajectory and submitted fine trace;
- candidate execution and passive runtime facts;
- earlier evidence from the same episode.

It does not see GT fine traces, invariant graphs, known PoCs, historical crash
states, held-out results, patches, or sanitizer traces supplied by the dataset.

The cross-sample Harness Patcher consumes the same canonical training
trajectory after an episode completes. Its isolated view contains:

- a complete isolated OpenHands source fork;
- the frozen full Subject trajectory, including factual Reward interactions;
- a value-free control-plane index into important lifecycle boundaries;
- generalized episode metrics and prior error history;
- prior harness version and Patch effectiveness.

Training trajectories can contain the public task semantics and source/tool
output already observed by the Subject. The Patcher does not receive GT,
known-success PoCs, hidden dataset sanitizer ground truth, or held-out episode
results. It cannot modify its trajectory/experience inputs, and source
validation rejects dataset-specific literals in a proposed OpenHands Patch.

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

A single logical read-only Reward Agent serves four task-local roles, but its
memory is the controller-owned files above rather than an unbounded model
conversation. Each model turn is fresh and ephemeral, reads the current
observation/evidence state, and therefore cannot fail because an old Codex
thread needs pre-sampling compaction:

1. compile Admission and Required/Observed/Transition assertions from public
   issue plus source;
2. observe the full trajectory and request submission at semantic readiness;
3. compile the frozen task assertions into source-valid passive probes;
4. turn trusted runtime evidence into factual, non-prescriptive feedback.

Assertion truth and polarity remain deterministic and controller-owned. A
Required assertion describes a safety obligation, so violation is vulnerable
progress; Observed and Transition assertions describe vulnerable facts, so
satisfaction is vulnerable progress. Admission alternatives use OR. Absence of
runtime evidence is `unresolved`, never silently treated as refutation.

## Experience Pool and multi-objective optimization

Every completed episode contributes exact metrics for:

- trigger success;
- no-submission behavior and first-submission sequence;
- semantic supervisor reminders, artifact-preparation reminders, and formal
  submission reminders;
- valid submissions occurring after a reminder and episodes where reminders
  never produce a candidate;
- total, unique, duplicate, and invalid submissions;
- Reward events followed by Subject action;
- distinct retries after Reward;
- ordered causal progress;
- instrumentation unavailability and premature finish attempts.

The Experience Analyzer may classify only enumerated, evidence-bound categories
such as `missing_submission`, `candidate_materialization_failure`,
`duplicate_candidate_loop`,
`reward_context_loss`, `productive_retry`, or `causal_stagnation`. The pool
stores successes as controls as well as failures. Harness optimization targets
submission conversion only after the semantic observer reports readiness; raw
reminder or submission count is never an objective, and duplicate/invalid
submission rates act as counter-objectives against over-eager submission.
The compact pool index references a frozen canonical trajectory for every
training episode. Control-plane records are navigation indexes, not substitutes
for the full trajectory and not additional model-generated traces.

## Cross-sample Harness Patcher

A separate workspace-write Codex role runs only after an episode ends. Every
patch deliberation starts in a fresh ephemeral model context and receives the
GT-free Experience Pool, referenced full training trajectories, previous error
history, and the current isolated OpenHands worktree; these controller-owned
files, not hidden conversation history, are its cross-sample memory. It may change real files
under OpenHands controller, CodeAct, memory, core loop, and prompt source. A
bounded proposal/contract-error/redeliberation loop rejects silent `keep`
decisions for high-confidence controller-owned failures. The controller checks:

- changed files exactly match the structured declaration;
- the Patch stays inside the allowed OpenHands harness surface;
- Python files parse and files are not deleted;
- no dataset/result/GT literals appear;
- controller-owned Experience Pool and trajectory inputs remain unchanged.

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
