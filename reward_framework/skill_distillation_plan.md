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
- Split: chronological TRAIN 300 / TEST 200, pinned in `gt_results/train_gt.json`
  and `gt_results/test_gt.json`.
- Operational batch size: 10 samples.
- Distillation window: the most recent 3 accepted operational batches, usually
  30 diagnoses, plus accumulated pools.
- Current default coding agent under improvement: DeepSeek Harness
  (`deepseek_harness`) with `deepseek-v4-flash`.
- Current default distillation roles: Codex subsession runner using
  `deepseek-v4-flash` through the configured model router/base URL.

The coding agent is the object being improved. Diagnostician, Teacher, Curator,
and Correction Agent are fixed measurement and distillation tools.

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

The skill packet has two skills.

### Reproduction Skill

`reproduction_skill/SKILL.md` contains:

- `R.A Reproduction Loop`: fixed. It gives a light loop: read the description,
  build a plausible PoC candidate early, submit, interpret feedback, revise or
  finalize.
- `R.B Learned Reproduction Lessons`: updateable. These are short behavioral
  biases for search, candidate construction, mutation, and path switching.

No other reproduction sections are currently writable. Do not reintroduce large
stage-level rule blocks unless the workflow is explicitly redesigned.

### Submission Skill

`submission_skill/SKILL.md` contains:

- `S.A Submission Loop`: fixed mechanical loop.
- `S.B Evidence-Gain Principle`: fixed. Submission feedback is evidence for the
  next candidate, not automatic final success.
- `S.C Learned Submission Lessons`: updateable. These are short behavioral
  biases about validation, feedback use, duplicate avoidance, and non-target
  crash handling.

Helpers are mechanical only:

- `submit_preflight.py`: file existence, exact duplicate detection,
  near-duplicate warning, structural summary.
- `submit_history.py`: candidate hash, analysis path, result path, status, and
  notes.

Helpers must not judge semantic evidence gain, force a submission, expose GT, or
change feedback semantics across harnesses.

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
   - It explains behavior, not whether the agent perfectly understood the bug.
   - It should answer how candidates were formed, whether submission was timely,
     how feedback affected later candidates, whether the agent refined one path
     or switched families, and why this helped or blocked reproduction.
   - It outputs a small `retry_recommendation` with natural-language summary and
     evidence.

4. Optional strict retry.
   - Retry only samples whose diagnosis says retry is worthwhile because the run
     was structurally healthy and the failure looks stochastic or near-miss.
   - Retry results go to a separate run id, for example
     `<batch_run_id>_retry1`.
   - Retry uses `--max-attempts 1`.
   - Merge retry into the primary batch result only when deterministic target
     success improves. Reachability-depth-only improvement is useful diagnostic
     evidence, but it must not overwrite the primary result.
   - Retry diagnoses may be passed to Teacher together with first-attempt
     diagnoses, so Teacher can compare first/retry behavior for the same sample.

5. Fold accepted batch evidence into pools.
   - Pools store sample-level diagnoses separated by deterministic outcome:
     success, failure/partial, non-target crash, and infrastructure.
   - Pools are Teacher memory. Do not maintain a separate memory file unless the
     design changes.

## Teacher

Teacher consumes:

- current skill packet;
- current lesson snapshot and capacity;
- diagnoses from the most recent 3 accepted operational batches;
- retry diagnoses when present;
- pools.

Teacher performs contrastive behavioral learning:

- successful or higher-progress behavior versus failed, partial, or non-target
  crash behavior;
- candidate construction versus candidate stagnation;
- useful feedback-driven mutation versus ignored feedback;
- productive path switching versus broad drift or wrong-path refinement;
- timely submission versus prolonged analysis without a concrete test.

Teacher may propose at most two concise updates. Allowed targets are only:

- `reproduction:R.B`
- `submission:S.C`

Teacher must not modify `R.A`, `S.A`, or `S.B`. Every proposal is a conditional
behavior bias: its condition must be observable from the public issue, source,
current trajectory, local diagnostics, submitted candidates, or runtime output
available during the run. Post-hoc reachability and inferred rankings such as
"best-reaching" may support training analysis, but cannot activate a lesson at
test time. Proposals must remain free of sample ids, project names,
file/function names, constants, GT labels, and evaluator terminology.

## Curator

Curator receives Teacher proposals and the current skill. It returns only:

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

Correction is the guardrail for whether `S_candidate` becomes the next accepted
skill.

The primary comparison is matched:

- compare `S_candidate` against the previous accepted skill `S_prev` on the same
  batch or validation panel;
- no-skill or historical full-run results are external references only, not the
  main acceptance gate for a learned update.

Decision rule:

- If `S_candidate` improves deterministic target success over `S_prev`, keep or
  apply the update.
- If `S_candidate` ties but shows cleaner behavior with no extra false positives
  or regressions, it may be kept only with explicit evidence.
- If `S_candidate` is worse, or retry does not recover target success, do not
  propagate it. Roll back to `S_prev` or correct/remove the responsible lesson.

When a candidate update fails the gate, rerun the same batch with the previous
accepted skill. The accepted rerun result becomes the batch result used for pools
and for the next Teacher window. Failed candidate-skill runs may be kept as
correction evidence, but they should not become the main training evidence.

## Batch-Level Control Flow

The portable DSH baseline split and per-batch summary are tracked at
`reward_framework/baselines/deepseek_harness_v4_flash/`. Local run outputs under
`reward_framework/harness_runs/` and `reward_framework/distillation_runs/` are
not portable by default.

Start DSH reward runs by initializing from that tracked baseline directory, for
example:

```bash
python -m reward_framework.distillation.cli init-run \
  --run-dir reward_framework/distillation_runs/dsh_deepseek \
  --baseline-dir reward_framework/baselines/deepseek_harness_v4_flash
```

For each batch `k`:

1. Start from accepted skill `S_k`.
2. Run batch `k` with `S_k`.
3. Evaluate, diagnose, optionally retry, and merge only target-success
   improvements.
4. Add accepted diagnoses to pools.
5. Teacher proposes updates from the latest 3 accepted batches plus pools.
6. Curator rewrites or skips proposals.
7. Apply accepted curation to form `S_candidate` for the next batch.
8. Validate `S_candidate` against `S_k` on a matched next batch or small panel.
9. If validation passes, promote `S_candidate` to accepted `S_{k+1}`.
10. If validation fails, roll back or correct, then rerun the same validation
    batch with the previous accepted skill before moving forward.

This means a non-improving update is not allowed to silently continue into later
batches.

## Artifacts

Canonical locations:

- `reward_framework/skill_packets/initial/`: hand-written initial packet.
- `reward_framework/distillation_runs/<run>/skill_packets/batch_XXX/`: accepted
  or candidate packets for a distillation run.
- `reward_framework/distillation_runs/<run>/batch_XXX/samples.txt`: batch sample
  list.
- `reward_framework/distillation_runs/<run>/batch_XXX/evaluation.json`: batch
  deterministic evaluation.
- `reward_framework/distillation_runs/<run>/batch_XXX/diagnoses.json`: behavior
  diagnoses for accepted first attempts.
- `reward_framework/distillation_runs/<run>/batch_XXX/retry/`: retry sample
  lists and merge reports.
- `reward_framework/distillation_runs/<run>/batch_XXX/teacher_candidate_updates.json`.
- `reward_framework/distillation_runs/<run>/batch_XXX/curator_decisions.json`.
- `reward_framework/distillation_runs/<run>/batch_XXX/correction_decision.json`.
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
- Do not merge retry results unless deterministic target success improves.
- Do not propagate a learned update that fails matched validation.
- Do not use a failed candidate-skill run as the main evidence for the next
  Teacher window.
