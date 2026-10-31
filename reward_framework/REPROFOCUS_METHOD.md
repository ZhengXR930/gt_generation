# ReproFocus: Diagnosis-Guided Self-Improvement

This document describes the implementation of the reward-framework learning
loop used by ReproFocus. It is deliberately written against the repository's
current contracts rather than against a historical five-file skill packet.

## 1. Problem and objective

The coding agent is asked to reproduce a public vulnerability by constructing
and submitting a concrete proof-of-concept input. The agent is stochastic and
usually receives only the public issue, source/workspace tools, local command
results, and the exact response from the submission harness. A binary success
count is too sparse for learning: a candidate can be rejected, accepted by the
parser, reach the relevant path, reach the vulnerable condition, or reach the
sensitive operation without triggering the target sanitizer.

ReproFocus therefore optimizes two linked objectives:

1. **Terminal objective:** the submitted candidate triggers the target
   vulnerability according to the canonical sanitizer oracle.
2. **Dense secondary objective:** when the terminal trigger is not obtained,
   the candidates reach a deeper target checkpoint and use returned feedback
   productively, with fewer duplicate or no-progress submissions.

The secondary objective is evidence for learning and regression detection. It
is not treated as a substitute for a correct target trigger.

The framework is harness/model configurable. The canonical experiments use
DeepSeek Harness + DeepSeek, but the same protocol can run through Codex,
OpenHands, Claude, or ZCode adapters. The harness, model, endpoint, token
budget, and reasoning setting are configuration fields; the learning roles do
not assume a particular coding harness.

## 2. Three-arm experimental design

Every paired comparison uses the same sample, prompt, workspace policy,
submission interface, evaluator version, and model configuration. Only the
skill packet differs:

| Arm | Packet | Purpose |
|---|---|---|
| `no_skill` | no packet installed | baseline coding-agent behavior |
| `initial_skill` | the blank two-file scaffold | measures the effect of the packet structure itself |
| `evolving` | the packet accepted from previous learning batches | measures learned lessons |

The first training batch has only `no_skill` and `initial_skill` controls; an
evolving arm does not exist before the first update. Later batches have all
three arms at the sample level. Candidate sequence numbers are **not** aligned
across arms: candidate 2 in one arm is not presumed to be the same PoC as
candidate 2 in another arm. Pairing is by sample, while each arm preserves its
own ordered candidate sequence.

The framework keeps one canonical result namespace per arm/configuration. Raw
run directories are execution artifacts; compact indexes record the packet
hash, prompt hash, split, model/harness, and result path needed for provenance.

## 3. Runtime skill packet

The active initial packet is
`skill_packets/dsh_deepseek_initial_two_layer_staged_100_100/`.
It installs exactly two Markdown skills together:

### `reproduction_skill/SKILL.md`

It contains four fixed stage headings, each with a neutral Goal:

1. `construct_input`: make the program accept the input and reach its parser;
2. `reach_vulnerable_code`: reach the code location where the defect lives;
3. `trigger_condition`: violate the safety condition so the failure manifests;
4. `verify_result`: interpret a returned behavior against the public issue.

### `submission_skill/SKILL.md`

It contains three fixed decision headings:

1. `when_to_submit`: decide when to produce and submit a candidate;
2. `after_feedback`: decide what to change after a result and which
   reproduction stage to revisit;
3. `when_to_stop`: decide when the run is finished.

The initial packet intentionally contains only these Goals and `No lesson yet.`
It is not the historical complete-flow packet and it is not the historical
five-file packet. Both runtime files are injected together; the coding harness
does not dynamically load one file at a time. The stage split is a planning
scaffold that lets the agent organize its reasoning without adding a
harness-specific procedure or a hidden evaluator signal.

The evolving packet has the same headings and fixed Goal text. Learned lessons
are inserted below the matching heading. A lesson is one physical line with
the following contract:

```text
**Use when:** <agent-visible condition>. **Do:** <one concrete action>. **Confirm / next:** <visible confirmation or next branch>.
```

The condition may use only the public issue/source, the agent's own commands
and local checks, or the exact returned submission response. It may not use a
sample id, project-specific anchor, private bytes, evaluator-only reachability,
GT labels, replay facts unavailable during search, or a fixed token/step quota.
Each stage has a capacity of three learned lessons. The packet updater prefers
revision/merging over unbounded growth. The packet hash is refreshed after
each materialized update, so a run can be tied to the exact skill contents.

## 4. Evidence model and the reachability ladder

The evaluator stores two logically separate axes for each candidate:

### Target ladder (offline evaluator axis)

The canonical checkpoint fields are independent:

* `R1_input_admitted` — the input is accepted by the parser/harness;
* `R2_source_reached` — execution reaches the relevant source path;
* `R3_root_cause_reached` — the vulnerable condition is reached;
* `R4_sink_reached` — the sensitive operation is reached;
* `R5_sanitizer_triggered` — the canonical sanitizer oracle says that the
  target vulnerability was triggered.

`R5` is deliberately independent from `R1`--`R4`: a trigger is determined by
the shared sanitizer oracle, not manufactured from debugger checkpoints. The
four debugger checkpoints are also independent; an exact-line miss at an
earlier checkpoint must not erase a later valid hit. This avoids treating a
checkpoint-coordinate problem as a failure of the coding agent.

### Observable behavior axis

The same candidate also receives an agent-behavior stage:

`none -> accepted_input -> relevant_path -> vulnerable_condition ->
sensitive_operation -> observable_failure -> target_issue`.

An abnormal exit or non-target sanitizer report is observable failure, not
automatically a target match. The evaluator records the returned exit code,
program/sanitizer output, replay observations, and the candidate's structural
change. Submission occurrences are preserved even when the evaluator
deduplicates identical PoCs, so exact repeats remain visible to learning.

For every adjacent candidate occurrence the framework computes:

* `stage_delta_from_previous` on the observable axis;
* `target_stage_delta_from_previous` on the evaluator target axis;
* `change_from_previous` (format, length/structure, byte/field value,
  different hypothesis, duplicate, or unknown);
* duplicate markers and the occurrence-to-unique-PoC join.

This distinction is central: a later crash in a different function may be a
changed hypothesis and an unchanged target depth, not target progress.

## 5. Batch lifecycle

Let `S_k` be the packet used for training batch `k`.

1. **Run generation.** Execute the coding agent on the fixed batch with `S_k`
   under the configured harness/model. The no-skill and initial controls use
   the same environment and prompt. Persist the full trajectory, manifest,
   per-submission candidate, runtime output, and `analysis.json`.
2. **Deterministic evaluation.** Evaluate every submitted candidate with the
   canonical evaluator. `evaluation.json` contains the per-sample rows,
   candidate occurrences, sanitizer-oracle trigger states, and target-depth
   summaries. A sanitizer crash or nonzero exit alone is not success.
3. **Diagnosis.** Run one Diagnostician role session per sample. It reads the
   sample's trajectory and candidate sequence together with the evaluator's
   offline facts. It produces the normalized sample diagnosis described below.
4. **Correction (for `k > 0`).** Audit whether the latest packet update caused
   a regression. If Correction returns `CORRECT` or `ROLLBACK`, materialize a
   candidate corrected/rolled-back packet, rerun the same batch, reevaluate and
   rediagnose it, and apply the machine propagation gate. A failed gate restores
   the pre-correction artifacts and packet.
5. **Pool update.** Add the accepted batch diagnoses to bounded TRAIN pools,
   retaining success, partial/failure, non-target-crash, and infrastructure
   cases as separate evidence classes.
6. **Teacher.** Read the current diagnosis window (normally the latest three
   accepted batches), the bounded pools, the current packet, and the current
   sample-level arm comparison. Produce findings and candidate updates.
7. **Curator.** Independently verify each Teacher proposal against the same
   diagnoses and paired arm evidence. Accept, revise, remove, or skip it.
8. **Apply.** Materialize accepted changes as `S_(k+1)`, refresh the packet
   metadata/hash, and record lineage. The next batch is the first experiment
   allowed to test the new packet.

The test split is evaluation-only: a frozen packet is run on the held-out
samples and no Teacher/Curator update is produced from those outcomes.

## 6. Diagnostician: single-sample causal normalization

The Diagnostician is not a lesson writer. It turns heterogeneous artifacts for
one sample into a structured causal account that later roles can compare.

### Inputs

The role workspace contains:

* `case_profile.json`: immutable evaluator-owned result branch;
* `candidate_sequence.json`: every submission occurrence in order, with
  target/observable stages, deltas, duplicate markers, structural change,
  replay observations, exit/crash facts, and visible feedback;
* the complete raw coding-agent transcript/session inventory;
* submitted PoCs, runtime logs, manifest, agent `analysis.json`, and
  reconstructed `context_visit.json`;
* `vulnerability_checklist.json`, sanitized reachability view, and compact
  reasoning/fine-trace diagnostics;
* an optional same-sample initial comparison.

The evaluator's reachability and target labels are explicitly marked as
offline facts. The role may use them for TRAIN-time attribution, but must not
claim that the coding agent saw them during search or that they were feedback.
Hidden reference PoCs, private target bytes, and evaluator-only source anchors
are not copied into the role workspace.

### Required analysis

The role audits (a) context loss, spinning, truncation, tool errors,
environment faults, submission timing, and stopping; (b) how far each
candidate reached; (c) what changed between candidates; and (d) how the agent
reacted to the returned feedback. It emits `progress_events` and
`turning_events`, then routes the sample to one offline `failure_stage`.

Routing priority is behavioral rather than a blind ladder lookup:

1. `submission:when_to_submit` for a ready candidate held too long, a
   feedback-free burst, or no submission despite a concrete candidate and a
   lost iteration opportunity;
2. `submission:after_feedback` for a repeated/non-material revision after
   returned evidence;
3. `submission:when_to_stop` for a voluntary stop while useful visible work
   remained;
4. `verify_result` for an actual false-positive interpretation or an
   unrecognized target-aligned crash;
5. otherwise map the unresolved first missing target rung to
   `construct_input`, `reach_vulnerable_code`, or `trigger_condition`;
6. use `unknown` when a confirmed success or the supplied evidence does not
   support a causal failure route.

The output is a single JSON object with `case`, fixed failure labels,
`failure_stage_evidence`, `harness_group`, `vulnerability_group`, the complete
`candidate_sequence`, progress/turning events, visible signals, explanations
for no submission/not reached/false positive/stopping/success, and explicit
uncertainty. This is the evidence contract for Teacher; it is not itself a
skill instruction.

## 7. Teacher: cross-sample contrast and proposal generation

Teacher receives:

* the current packet and lesson snapshot;
* the per-sample diagnoses from the current diagnosis window;
* bounded historical pools;
* `teacher_evidence.json` and separate arm files under
  `paired_controls/{initial_skill,evolving,no_skill}.json` when available.

Each arm file contains agent-visible trace excerpts around submissions,
candidate order and structural summaries, duplicate information, exact
returned feedback, and the Diagnostician's explanation. Raw evaluator ladders
and target labels are kept out of the role-visible paired evidence; the
Diagnostician and Correction are the components that use evaluator-owned
reachability facts. This separation prevents Teacher from turning a post-hoc
rank into a test-time condition.

Teacher performs two contrasts:

1. **Within-sample:** which candidate change and returned observation preceded
   an advance, a productive hypothesis switch, a false-positive correction, or
   an unchanged/duplicate submission?
2. **Across-sample:** which such mechanisms recur across unrelated projects and
   explain either target success or the denser target-depth signal?

Teacher must report every recurring pattern in `findings`, including patterns
already covered by the current packet. It emits `candidate_updates` only for
uncovered, transferable mechanisms. Each proposal is assigned to exactly one
of the seven runtime stages and includes:

* `proposal`: the one-line three-part lesson;
* `observation_evidence`: agent-visible condition/change/feedback;
* `counterevidence`: where the pattern is uncertain or fails;
* `proposal_rationale`: what it adds beyond existing lessons;
* `regression_risk`: how it might over-constrain search.

This makes Teacher a contrastive learner, not a summarizer of sanitizer types.

## 8. Curator: evidence and safety gate

Curator receives the complete Teacher payload (both `findings` and
`candidate_updates`), current packet, lesson snapshot, diagnoses, bounded
pools, and the same separated paired-arm evidence. It rechecks causal claims
itself rather than trusting Teacher's prose.

A proposal can be `MODIFY` only when it has:

1. a condition observable by the coding agent;
2. one concrete action;
3. a visible confirmation or next branch;
4. diagnosis/evidence support beyond a generic summary; and
5. a decision branch not already represented in that stage.

Curator returns `SKIP` for evaluator-only or project-specific rules,
duplicates, unsupported causal claims, vague conditions, or rules that shorten
productive iteration without a visible switch condition. `REMOVE` is reserved
for repeated evidence that an existing lesson is harmful or unnecessarily
restrictive. Curator acceptance authorizes a bounded next-batch test; it is not
a claim that the lesson works.

`apply-curation` copies the packet, writes accepted lesson bullets beneath the
target headings, preserves fixed Goal text, records `applied_updates.json` and
`CHANGELOG.md`, and refreshes `PACKET_META.json`.

## 9. Correction: matched regression and propagation control

Correction is the guardrail for the packet that has just been used. It is not
another Teacher and cannot add a new lesson. It receives:

* previous/current packets and lesson snapshots;
* `applied_updates.json` lineage;
* current batch diagnoses and compact batch evaluation;
* the cumulative `rolling_comparison.json` over recent matched batches;
* current three-arm paired evidence and optional baseline evaluations;
* bounded pools.

Its primary comparison order is:

1. per-sample deepest target depth;
2. reproduced samples (canonical sanitizer trigger);
3. duplicate submissions;
4. voluntary stops with budget left, when measured.

It also inspects efficiency costs: no-progress submissions and rate,
productive submissions, target-depth advances, and target-depth gain per
submission. A changed hypothesis is not penalized merely because it stays at
the same stage; repeated unchanged candidates and duplicate submissions are
different signals from productive exploration.

Correction returns:

* `KEEP` when there is no attributed regression;
* `CORRECT` with modifications/removals to existing lessons when the trace
  links a regression to them;
* `ROLLBACK` when the latest update harms broadly or cannot be attributed to a
  specific lesson.

The machine gate in `cli.py` independently recomputes the pre/post metrics. It
accepts a correction only if the roster is comparable, no sample loses target
depth, aggregate GT success does not decrease, duplicate submissions do not
rise, and a no-progress cost increase is not paired with lower depth efficiency
without an outcome gain. If not, it restores pre-correction evaluation,
diagnoses, and packet artifacts. This prevents a plausible role answer from
silently propagating a harmful update.

## 10. Main artifacts and provenance

For batch `k`, the durable learning artifacts are:

* `evaluation.json` — deterministic per-sample evaluator rows;
* `diagnoses.json` and `diagnosis_status.json` — normalized sample diagnoses;
* `rolling_comparison.json` — matched multi-batch initial/evolving/no-skill
  outcome/efficiency view for Correction;
* `teacher_evidence.json` — the paired evidence index shared with roles;
* `teacher_candidate_updates.json` — Teacher findings and proposals;
* `curator_decisions.json` — Curator gate decisions;
* `correction_decision.json` and `correction_gate.json` — role and machine
  propagation decisions;
* `applied_updates.json` — packet lineage;
* `skill_packets/batch_NNN/` — the exact packet for the next trial;
* `pools.json` — bounded TRAIN memory used by Teacher and Curator.

Raw harness artifacts remain under `reward_framework/harness_runs/`; compact
stable arm indexes live under `reward_framework/results/`. Tokens and endpoint
values are loaded at runtime from the local credential file and are not stored
in configs or committed artifacts.

## 11. Command-level execution

The single orchestration entry point is
`python -m reward_framework.distillation.cli`. The important subcommands are:

```bash
# create a run from the pinned split and initial packet
python -m reward_framework.distillation.cli init-run \
  --run-dir reward_framework/distillation_runs/<run> \
  --baseline-dir reward_framework/baselines/dsh_deepseek_failure_balanced100

# one complete training range (generation, evaluation, diagnosis,
# correction/propagation, Teacher, Curator, packet application)
python -m reward_framework.distillation.cli train-batches \
  --run-dir reward_framework/distillation_runs/<run> \
  --start-batch 0 --end-batch 9
```

The steps can also be run individually with `run-batch`, `evaluate-batch`,
`diagnose-batch`, `correct-skill`, `apply-correction`, `propose-updates`,
`curate-updates`, and `apply-curation`. `run-test` and `evaluate-test` execute
the frozen held-out arm without modifying the packet.

## 12. Interpretation and limitations

The method does not claim that every accepted lesson improves every sample.
The same-sample triplets and rolling window are controls for stochasticity,
not proof of deterministic causality. The strongest claim supported by the
framework is that a lesson is a transferable, agent-observable hypothesis that
survived evidence checks and a bounded next-batch regression gate.

The most important separation is therefore:

```text
agent trace + returned feedback
        -> Diagnostician (single-sample causal normalization)
        -> Teacher (cross-sample findings/proposals)
        -> Curator (safety/evidence gate)
        -> packet S_(k+1)
        -> same-batch Correction/propagation gate
        -> next-batch test
```

Evaluator-owned reachability and sanitizer results make the learning signal
measurable, but they are never silently converted into a test-time lesson
condition.
