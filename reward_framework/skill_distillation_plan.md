# Reward Framework Skill Distillation Plan

## Scope

This framework distills reusable PoC reproduction skills from the `valid_gt.json` corpus. The 500 samples are ordered by the committer date of each sample's `vulnerable_commit` — the state of the repository the agent is actually handed. The earliest 300 are the training set, the most recent 200 are the held-out test set. Training is processed as 30 batches of 10 samples. The default coding-agent run for each training batch is Codex with `gpt-5.5-2026-04-24`.

The ordering was computed once from `dataset/commit_dates.json` and is **frozen** into `gt_results/train_gt.json` and `gt_results/test_gt.json`, next to the `valid_gt.json` denominator they partition. Everything downstream reads those files; nothing recomputes the split. That matters because a split recomputed per run would silently change the moment another date was resolved, and results either side of that change would not be comparable.

Each frozen file carries every sample's date and which field it came from, so "why is this sample in train" is answerable from the split alone. Of the 500 samples, 442 are ordered by their vulnerable-commit date, 29 by their fix-commit date, and 10 by the fix date recorded in the local ARVO patch. The remaining 19 could not be dated — their upstreams are unreachable from the evaluation network or refuse fetch-by-sha — and sit at the tail, which places them in the held-out set; their position within the test set carries no chronological meaning.

Projects overlap between train and test. That is deliberate: this measures whether skills learned from past vulnerabilities help on later ones, not out-of-distribution transfer. It is only safe because ground-truth text cannot reach the skill packet — see Packet Isolation.

## Initial Skill Packet

The initial packet contains two skills.

### Reproduction Skill

`reproduction_skill/SKILL.md` has no helper scripts. It contains:

- R.A Reproduction Loop Definition: fixed workflow `Issue + Code -> Hypothesis -> Candidate -> Attempt -> Feedback -> Revise`. Distillation must not rewrite this loop.
- R.B Vulnerability Hypothesis Construction: updateable guidance for using Parser, Source, Root Cause, Sink, and Trigger evidence to form better hypotheses.
- R.C Learned Lessons: the primary learning area.
  - R.C1 Stage-level Lessons: Parser, Source, Root Cause, Sink, Trigger lessons.
  - R.C2 Cross-stage Lessons: preserving progress when moving between stages.
  - R.C3 Vulnerability-type Lessons: lessons organized by vulnerability class.

### Submission Skill

`submission_skill/SKILL.md` contains:

- S.A Submission Loop: fixed workflow `Candidate -> readiness reasoning -> submit -> record result -> return evidence to reproduction`.
- S.B Evidence-Gain Gate: fixed principle with updateable strategy. Semantic evidence gain remains in agent reasoning, not in helper code.
- S.C Learned Submission Lessons: updateable lessons distilled from trajectories.

Submission helpers are limited to `submit_history.py` (persist candidate identity, status, notes) and `submit_preflight.py` (existence, exact duplicate, near-duplicate warning, structural summary). Helpers must not judge semantic evidence gain or use hidden GT/reachability information.

Skill documents name runtime locations through `${HELPERS_DIR}` and `${STATE_DIR}` only. Each adapter installs the packet in its own native form and resolves those placeholders at install time, so one distilled artifact stays valid under every harness.

## Lesson Addressing and Capacity

Every lesson in a writable section carries a stable id (`- [R.C1#3] ...`). A curator decision either replaces an addressed lesson or appends a new one. Each section has a capacity; once it is full, the only way to add something is to replace something. This is what keeps the packet sharp instead of letting it grow into a contradictory pile across 30 batches.

Writable targets: `reproduction:R.B`, `reproduction:R.C1`, `reproduction:R.C2`, `reproduction:R.C3`, `submission:S.B`, `submission:S.C`. R.A and S.A are not addressable at all.

## Batch Workflow

For each 10-sample training batch:

1. Run the coding agent with the current skill packet.
2. Evaluate each sample with the three GT-derived diagnostics:
   - **fine-trace coverage** — node and edge recall over the GT trace DAG: what fraction of GT steps the agent's `analysis.json.fine_trace` recovered, and what fraction of the GT `depends_on` dependencies it recovered with both endpoints in causal order. A node is recovered or it is not; there is no weighting or partial credit (static);
   - **vuln_logic reasoning** — whether the agent named the right source, obligation, sink, and propagation, scored against `verified_invariants.json` with field-binding normalization (static);
   - **location reachability** — how far the submitted PoC actually ran, as the R1..R5 ladder (dynamic).
3. Derive the outcome deterministically from reachability: `success`, `partial`, `failure`, or `infrastructure`, plus `deepest_stage` and `first_failed_stage`. No model decides this.
4. The diagnostician reads the trajectory, the public description, the diagnostics, and the deterministic verdict, then explains *why* the run stopped where it did.
5. Fold each batch into `pools.json`: sample-level failure and success diagnosis records, plus derived modes keyed by stage/vulnerability type for recurrence gating. The pools are the Teacher memory; there is no separate memory file.
6. The Teacher reads the pools, the current lessons with their ids and remaining capacity, and this batch's diagnoses, then proposes updates for modes that clear the recurrence gate.
7. The Curator returns `MODIFY` or `SKIP` for each proposal, rewriting the wording it accepts.
8. Applied decisions produce the packet used by the next batch.

## Roles Are Subsessions

The diagnostician, Teacher, and Curator are agents, not one-shot completions. They are fixed measurement and distillation tools, run as Codex subsessions with the distiller model. The coding agent is the object being improved and remains configurable by harness and model: the default is Codex with `gpt-5.5-2026-04-24`, while later experimental arms can use DSH+DeepSeek or another harness without changing the distillation roles. Each role gets a working directory of input files and writes one `OUTPUT.json`. The diagnostician therefore reads the entire trajectory rather than a truncated excerpt, and every role can grep and diff its inputs.

`harness_runtime/subsession.py` owns the neutral executor; the benchmark runner delegates to it, so there is one implementation of the bridge and the `codex exec` invocation. Role workspaces get a clean `CODEX_HOME`: the PoC skills belong to the agent under study, not to the roles studying it.

## Packet Isolation

The diagnostician and Teacher may read ground truth: on the training split that is label access. What must never happen is ground-truth text reaching `SKILL.md`, because that file is an input to the agent at test time.

The boundary is the `proposal` field. Only `proposal` is written into the packet, and it passes `reward_framework/distillation/lint.py` first, which rejects source-file names, `file:line` anchors, hex and long numeric constants, sample ids, and any identifier or code fragment drawn from the local ground-truth corpus. `evidence` and `rationale` stay in the run artifacts. The packet-local changelog records ids and targets only.

`audit-plan` checks this functionally: it asserts the lint rejects a real ground-truth function name, asserts it accepts a generic transferable lesson, and asserts sentinel evidence strings do not survive into a generated packet.

## Held-out Evaluation

`run-test` runs the 200 test samples once with a frozen packet and no learning; `evaluate-test` reports the outcome distribution and trigger rate. Arms:

- `bare` — the same scaffold with every learned lesson stripped, isolating the effect of the lessons from the effect of having a skill at all;
- `initial` — the hand-written initial packet;
- `distilled` — the packet after the final training batch.

The no-packet baseline is the existing `poc_generation/` pipeline, which does not import the reward adapters.

## Implementation Targets

- `reward_framework/skill_packets/initial/` is the canonical initial packet.
- `reward_framework/distillation/` owns splits, batch orchestration, deterministic outcomes, pools, role prompts, packet lint, and update application.
- `evaluator/reasoning/fine_trace_coverage.py` owns deterministic fine-trace coverage scoring.
- `evaluator/evaluate.py` includes fine-trace coverage in per-sample and batch summaries.
- Reward adapters expose the same task protocol as normal PoC generation plus the skill packet, and must not append README content or forced-submit constraints.
