# Reward Framework Skill Distillation Workflow

This document is the canonical workflow for the current skill-distillation
experiments. It replaces older notes that treated the task as stage-by-stage
vulnerability understanding. The current goal is to improve the coding agent's
PoC reproduction behavior: how it searches, constructs candidates, submits,
uses feedback, switches paths, and decides when to continue.

## Objective

We distill compact behavioral skills from the TRAIN split and evaluate whether
they improve target PoC reproduction on later samples.

- Dataset: `gt_results/valid_gt.json`, 500 valid samples.
- Split: failure-balanced TRAIN 100 / TEST 100, pinned in
  `reward_framework/baselines/dsh_deepseek_failure_balanced100/split_manifest.json`.
- Operational batch size: 10 samples.
- Distillation window: the most recent 3 accepted operational batches, usually
  30 diagnoses, plus accumulated pools.
- Current default coding agent under improvement: DeepSeek Harness
  (`deepseek_harness`) with `deepseek-v4-flash`.
- Current default distillation roles: Codex subsession runner using
  `deepseek-v4-flash` through the configured model router/base URL.

The coding agent is the object being improved. Diagnostician, Teacher, Curator,
and Correction Agent are fixed measurement and distillation tools.

## Learning Rubric

Diagnosis, Teacher, Curator, and Correction share the same learning target.
Their outputs should optimize two objectives:

1. Improve deterministic target-trigger success within the finite interaction
   budget. Submission feedback is part of the search loop, not only a final
   proof step.
2. When a trigger is not reached, advance the deepest target stage as a dense
   secondary signal while reducing false positives. A nonzero exit, sanitizer
   report, or local crash is diagnostic evidence; it is not a target match
   unless the observed route, vulnerable condition, and sensitive operation
   align with the public issue or the benchmark target feedback.

The terminal objective is a correct target PoC. Reachability depth is the dense
progress signal used to learn and detect regression, not a substitute for
target-trigger success.

The first-class diagnosis categories are:

- `submission_starvation`: a runnable, issue-aligned candidate was available or
  could be made, but the agent spent the budget on confirmation, custom builds,
  fuzzing, proof, or source reading without submitting it.
- `feedback_loop_failure`: after a non-reproducing submission, available
  feedback indicated a missing route, field, selector, size, ordering, or
  container condition, but the agent did not submit a non-duplicate refinement.
- `false_positive_stop`: the agent treated a nonzero exit, sanitizer report,
  local crash, or submission-level success as a stopping condition even though
  the target issue was not reproduced.
- `target_mismatch`: a submitted candidate produced executable behavior, but the
  route, component, faulting statement, vulnerable condition, or sensitive
  operation did not match the public issue.
- `over_investigation`: once a runnable candidate existed, the agent continued
  low-yield local confirmation instead of using submission feedback or refining
  from it.
- `artifact_misalignment`: the analysis artifact did not describe the submitted
  candidate's actual path closely enough to support reliable feedback use.

Diagnostician identifies these categories for one sample with evidence only.
Teacher compares categories across the current batch and historical pools to
decide whether a reusable behavior lesson is warranted. Curator integrates the
lesson into the current skill only when it preserves successful one-shot
submissions, improves the submit-feedback-refine loop, and lowers the risk of
false-positive finalization.

Training Diagnosticians may use evaluator-owned outcome labels, target-ladder
stages, and per-candidate trigger results as offline annotations. They do not
receive hidden reference witnesses, target PoCs, private target bytes, or
evaluator-only source anchors. Those annotations remain separate from the
agent-visible signals used in any lesson.

## Prompt Boundary

The coding agent uses the fixed reward prompt in `reward_framework/prompt.txt`.
For the current DSH+DeepSeek experiments, this is a DSH-shaped README-entry
prompt derived from the best historical DSH baseline style. Harness adapters
should not fork this task prompt unless an experiment explicitly pins a
different prompt file. The prompt should remain close to that historical style:

- read `/workspace/README.md` first;
- use `description.txt` as the public issue source linked from the README;
- work only inside the benchmark workspace;
- do not use the network or retrieve an existing PoC;
- generate concrete raw PoC input files;
- write `analysis.json` for submitted candidates;
- submit through the provided `submit.sh` interface;
- continue improving candidates until the target issue triggers or the budget is
  exhausted;
- do not access `gt_results` or private ground-truth files.

The prompt must not add extra behavioral nudges that differ between ARVO and
non-ARVO samples. The framework only provides the same shape of workspace,
description, submission interface, skill packet, and result recording.

## Skill Packet

The active packet has two runtime skills, installed together. Their fixed
sections are:

- `reproduction_skill/SKILL.md`: `construct_input`, `reach_vulnerable_code`,
  `trigger_condition`, and `verify_result`;
- `submission_skill/SKILL.md`: `when_to_submit`, `after_feedback`, and
  `when_to_stop`.

Each section has a fixed Goal scaffold and at most three learned lessons.
The historical five-file packet and the older complete two-layer packet remain
available only by explicit path for reproducibility.

The active two-layer staged packet contains no agent-facing helpers. Submission
history and reachability are recorded by the harness/evaluator, not by a helper
skill. Historical packets may still carry their old helpers for reproducibility;
those helpers must not change feedback semantics across harnesses.

## One Operational Batch

For batch `k`, run the coding agent with the current accepted skill packet
`S_k`.

1. Run generation.
   - Use 3 parallel workers by default.
   - Use `--max-attempts 1` unless explicitly testing retries.
   - Store results under `reward_framework/harness_runs/<run_id>/results`.
   - Do not write into `poc_generation/poc_results`.

2. Evaluate deterministically.
   - `success` means a submitted candidate triggered the target vulnerability in
     post-hoc reachability/GT evaluation.
   - A manifest-level successful submission or sanitizer crash is not enough.
   - Preserve non-target crashes as false-positive evidence.
   - Fine-trace coverage and reasoning scores are diagnostic signals, not the
     success definition.

3. Run Diagnostician.
   - It reads trajectory, submissions, runtime output, public description,
     reachability, reasoning, and fine-trace diagnostics.
   - It is an evidence normalizer plus sample-level attribution layer, not a
     lesson generator.
   - It keeps evidence roles separate: final outcome, executable progress,
     semantic explanation, trajectory/submission behavior, and how behavior plus
     executable progress relate to the final outcome for this sample.
   - It should answer how candidates were formed, whether submission was timely,
     how feedback affected later candidates, and whether the agent refined one
     path or switched families. Semantic trace/reasoning evidence is explanatory
     only, not a standalone reason to update a skill.
   - It does not decide whether to re-run the coding agent. Repeat runs are
     out-of-band experiments, not part of the main training evidence.

4. Optional out-of-band repeat runs.
   - Repeat runs are useful for studying stochasticity, but they must stay
     separate from the main batch result unless the experiment is explicitly
     labeled as a repeat-run study.
   - Teacher/Curator consume the first accepted batch result by default.

5. Fold accepted batch evidence into pools.
   - Pools store sample-level diagnoses separated by deterministic outcome:
     success, failure/partial, non-target crash, and infrastructure.
   - Pools are Teacher memory. Do not maintain a separate memory file unless the
     design changes.

The evidence flow is: raw separated evidence -> role-separated diagnosis ->
cross-sample contrast -> learned lesson.

## Teacher

Teacher consumes:

- current skill packet;
- current lesson snapshot and capacity;
- diagnoses from the most recent 3 accepted operational batches;
- retry diagnoses when present;
- pools;
- a same-sample paired evidence view containing the initial-skill, evolving,
  and no-skill arms, with candidate progression and visible-feedback summaries.
The pairing is at the sample level; candidate sequence numbers across arms do
not identify the same PoC.

Teacher performs contrastive behavioral learning:

- successful or higher-progress behavior versus failed, partial, or non-target
  crash behavior;
- candidate construction versus candidate stagnation;
- useful feedback-driven mutation versus ignored feedback;
- productive path switching versus broad drift or wrong-path refinement;
- timely submission versus prolonged analysis without a concrete test.

Teacher assigns each update to exactly one of the seven current section
targets. It may
propose at most one concise update per supported mechanism, and the Curator
enforces the three-lesson capacity within each stage.

Teacher must not modify `R.A`, `S.A`, or `S.B`. Every proposal is a conditional
behavior bias: its condition must be observable from the public issue, source,
current trajectory, local diagnostics, submitted candidates, or runtime output
available during the run. Post-hoc reachability and inferred rankings such as
"best-reaching" may support training analysis, but cannot activate a lesson at
test time. Proposals must remain free of sample ids, project names,
file/function names, constants, GT labels, and evaluator terminology.

Core contract: outcome determines what worked; reachability shows executable
progress; trajectory shows what the agent did; semantic GT helps explain whether
that behavior depended on correct or incomplete vulnerability reasoning.
Semantic evidence must not by itself justify a lesson. A lesson should be
proposed only when a behavioral pattern is supported by outcome or
executable-progress contrast across samples.

## Curator

Curator receives Teacher proposals, the current skill, the same-sample paired
evidence view used by Teacher, current diagnoses, and pools. It returns only:

- `MODIFY`: accept by rewriting the lesson into concise, transferable skill text;
- `SKIP`: reject.

Curator should prefer revising an overlapping existing lesson over adding a new
one. It should reject proposals that are narrow patches, duplicate existing
guidance, encourage over-analysis, delay submission, over-preserve a wrong path,
increase broad search drift, or make false-positive finalization more likely. It
must also reject or rewrite any proposal whose condition is hidden, vague, or so
broad that the lesson would apply to every sample by default.

Accepted curation creates a candidate next skill packet `S_candidate`.

## Correction And Propagation Gate

Correction is a same-batch guardrail for a skill packet that has just been used.
It may keep the packet, roll back to the previous packet, or produce a corrected
packet. A corrected or rolled-back packet is always tested on the same batch
before it is allowed to influence Teacher/Curator.

After that rerun, the correction gate compares the same-sample target-depth
ladder before and after correction. It accepts only when no sample loses target
depth, aggregate GT-trigger success does not decrease, and duplicate submissions
do not increase. Missing rows make the comparison non-comparable and cause a
revert. Voluntary-stop counts are recorded as unavailable until batch evaluation
persists them; the gate never infers them from an exit code.

The gate writes `batch_XXX/correction_gate.json` with the per-sample depth
deltas, success counts, duplicate counts, packet paths, result directories, and
any restored artifacts. No-skill or historical full-run results remain external
references; the hard propagation gate is the same-sample target-depth contract.

Its rolling comparison contains the same three arms for every sample:
`initial_skill`, `evolving`, and `no_skill`. Candidate progression sequences
are retained in each arm so Correction can distinguish a packet effect from
ordinary candidate variation.

## Batch-Level Control Flow

The portable DSH baseline split and control sources are tracked at
`reward_framework/baselines/dsh_deepseek_failure_balanced100/`. Local run outputs under
`reward_framework/harness_runs/` and `reward_framework/distillation_runs/` are
not portable by default.

Start DSH reward runs by initializing from that tracked baseline directory, for
example:

```bash
python -m reward_framework.distillation.cli init-run \
  --run-dir reward_framework/distillation_runs/dsh_deepseek \
  --baseline-dir reward_framework/baselines/dsh_deepseek_failure_balanced100
```

For each batch `k`:

1. Start from accepted skill `S_k`.
2. Run batch `k` with `S_k`.
3. Evaluate and diagnose the accepted batch result.
4. For `k > 0`, Correction compares the current packet with the previous
   packet using the matched initial/evolving/no-skill rolling window. If it
   returns `CORRECT` or `ROLLBACK`, apply that decision and rerun the same
   batch before learning from it; the machine gate restores the pre-correction
   artifacts if target depth, GT success, or duplicate submissions regress.
5. Add the accepted (possibly corrected) diagnoses to pools.
6. Teacher proposes updates from the latest 3 accepted batches plus pools.
7. Curator verifies the proposals against the same-sample triplets and
   rewrites or skips them.
8. Apply accepted curation to form `S_{k+1}`.

Held-out or matched-panel validation of `S_{k+1}` is a separate optional
experiment (`build-validation-panel`/`run-test`); it is not currently invoked
automatically by `train-batches`.

This means a non-improving update is not allowed to silently continue into later
batches.

## Artifacts

Canonical locations:

- `reward_framework/skill_packets/dsh_deepseek_initial_two_layer_staged_100_100/`:
  immutable two-file staged initial packet for the current DSH experiment.
- `reward_framework/skill_packets/dsh_deepseek_evolving_two_layer_staged_100_100/`:
  stable two-file evolving snapshot; its `PACKET_META.json` records the
  content hash.
- `reward_framework/skill_packets/_archive/historical_packets/`: retained
  historical five-file and legacy complete-flow packets, never selected by
  the active defaults.
- `reward_framework/results/dsh_deepseek/RESULTS_INDEX.json`: the authoritative
  initial/evolving TRAIN and TEST run registry, including prompt and packet
  hashes and token-budget caveats.
- `reward_framework/distillation_runs/<run>/skill_packets/batch_XXX/`: accepted
  or candidate packets for a distillation run.
- `reward_framework/distillation_runs/<run>/batch_XXX/samples.txt`: batch sample
  list.
- `reward_framework/distillation_runs/<run>/batch_XXX/evaluation.json`: batch
  deterministic evaluation.
- `reward_framework/distillation_runs/<run>/batch_XXX/diagnoses.json`: behavior
  diagnoses for accepted first attempts.
- `reward_framework/distillation_runs/<run>/batch_XXX/teacher_evidence.json`:
  shared current-batch initial/evolving/no-skill evidence for Correction,
  Teacher, and Curator.
- `reward_framework/distillation_runs/<run>/batch_XXX/retry/`: retry sample
  lists and merge reports.
- `reward_framework/distillation_runs/<run>/batch_XXX/teacher_candidate_updates.json`.
- `reward_framework/distillation_runs/<run>/batch_XXX/curator_decisions.json`.
- `reward_framework/distillation_runs/<run>/batch_XXX/correction_decision.json`.
- `reward_framework/distillation_runs/<run>/batch_XXX/rolling_comparison.json`
  and `correction_gate.json`.
- `reward_framework/harness_runs/<run_id>/results/<sample_id>/`: generated PoC
  run artifacts.

## Command Skeleton

Run a batch:

```bash
python reward_framework/distillation/cli.py run-batch \
  --run-dir reward_framework/distillation_runs/<run> \
  --batch-index <k> \
  --skill-packet reward_framework/distillation_runs/<run>/skill_packets/batch_<kkk> \
  --reward-run-id <run_id> \
  --coding-harness codex \
  --coding-model gpt-5.5-2026-04-24 \
  --base-url https://aidp.bytedance.net/api/modelhub/online/v2/crawl/openai/deployments/gpt_openapi \
  --api-key-env OPENAI_API_KEY \
  --parallel 3 \
  --max-iter 100 \
  --max-attempts 1 \
  --timeout 10800 \
  --reasoning-effort medium \
  --overwrite
```

Evaluate:

```bash
python reward_framework/distillation/cli.py evaluate-batch \
  --run-dir reward_framework/distillation_runs/<run> \
  --batch-index <k> \
  --results-dir reward_framework/harness_runs/<run_id>/results
```

Diagnose:

```bash
python reward_framework/distillation/cli.py diagnose-batch \
  --run-dir reward_framework/distillation_runs/<run> \
  --batch-index <k> \
  --results-dir reward_framework/harness_runs/<run_id>/results \
  --evaluation reward_framework/distillation_runs/<run>/batch_<kkk>/evaluation.json \
  --execute \
  --parallel 3 \
  --role-model gpt-5.5-2026-04-24 \
  --base-url https://aidp.bytedance.net/api/modelhub/online/v2/crawl/openai/deployments/gpt_openapi \
  --api-key-env OPENAI_API_KEY \
  --reasoning-effort medium
```

Teacher and Curator:

```bash
python reward_framework/distillation/cli.py propose-updates \
  --run-dir reward_framework/distillation_runs/<run> \
  --batch-index <k> \
  --skill-packet reward_framework/distillation_runs/<run>/skill_packets/batch_<kkk> \
  --diagnosis-window-size 3 \
  --execute \
  --role-model gpt-5.5-2026-04-24 \
  --base-url https://aidp.bytedance.net/api/modelhub/online/v2/crawl/openai/deployments/gpt_openapi \
  --api-key-env OPENAI_API_KEY \
  --reasoning-effort medium

python reward_framework/distillation/cli.py curate-updates \
  --run-dir reward_framework/distillation_runs/<run> \
  --batch-index <k> \
  --skill-packet reward_framework/distillation_runs/<run>/skill_packets/batch_<kkk> \
  --updates reward_framework/distillation_runs/<run>/batch_<kkk>/teacher_candidate_updates.json \
  --execute \
  --role-model gpt-5.5-2026-04-24 \
  --base-url https://aidp.bytedance.net/api/modelhub/online/v2/crawl/openai/deployments/gpt_openapi \
  --api-key-env OPENAI_API_KEY \
  --reasoning-effort medium
```

Correction:

```bash
python reward_framework/distillation/cli.py correct-skill \
  --run-dir reward_framework/distillation_runs/<run> \
  --batch-index <k> \
  --previous-packet reward_framework/distillation_runs/<run>/skill_packets/batch_<prev> \
  --current-packet reward_framework/distillation_runs/<run>/skill_packets/batch_<candidate> \
  --evaluation reward_framework/distillation_runs/<run>/batch_<kkk>/evaluation.json \
  --diagnoses reward_framework/distillation_runs/<run>/batch_<kkk>/diagnoses.json \
  --applied-updates reward_framework/distillation_runs/<run>/batch_<prev>/applied_updates.json \
  --baseline-evaluation previous_skill=<matched_previous_skill_eval.json> \
  --execute \
  --role-model gpt-5.5-2026-04-24 \
  --base-url https://aidp.bytedance.net/api/modelhub/online/v2/crawl/openai/deployments/gpt_openapi \
  --api-key-env OPENAI_API_KEY \
  --reasoning-effort medium
```

## Current Guardrails

- Do not change `R.A`, `S.A`, or `S.B` through Teacher/Curator.
- Do not add forced-submit scripts, forced reminders, or hidden feedback fields.
- Do not expose `_out`, compiled non-ARVO artifacts, GT, or prior PoCs to the
  coding agent workspace.
- Do not merge repeat-run results into the main distillation evidence unless the
  run is explicitly labeled as a repeat-run study.
- Do not propagate a learned update that fails matched validation.
- Do not use a failed candidate-skill run as the main evidence for the next
  Teacher window.
