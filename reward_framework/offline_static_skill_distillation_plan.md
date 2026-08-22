# Offline Static Skill Distillation Plan

## Goal

Build a static two-layer OpenHands skill packet from already completed GT runs, then freeze it before test evaluation. This phase verifies whether static skills distilled from historical trajectories and GT diagnostics can improve PoC success without per-test adaptation.

## Dataset policy

- Use `gt_results/train_gt.json` as the learning set: 300 chronological training samples.
- Use `gt_results/test_gt.json` as the frozen evaluation set: 200 later samples.
- Do not use a dev split in this phase.
- Do not inspect test trajectories for skill writing, prompt edits, helper edits, or model selection.

## Architecture

```text
Raw train artifacts
  - issue description
  - trajectory
  - submit history
  - submitted PoC candidates
  - submitted analysis.json
  - reasoning diagnostics per submit
  - reachability diagnostics per submit
        ↓
Observation Packager: trim-only Markdown, no semantic predicates
        ↓
Teacher / Proposal Writer: TraeCLI + GPT-5.5
        ↓
Evolver / Curator: TraeCLI + GPT-5.5, ACCEPT/MERGE/MODIFY/SKIP by skill chunk/helper
        ↓
Skill Patcher: TraeCLI + GPT-5.5, minimal skill/helper edits
        ↓
Audit: independent TraeCLI + GPT-5.5
        ↓
Frozen static skill packet
        ↓
Final test evaluation only
```

## Chunk-level skill packet design

Use a small number of stable skill chunks rather than many micro-blocks. Edits should be large enough to carry a complete procedure, but small enough to add/merge/replace without rewriting the whole skill.

Level 1 chunks:

- L1.A submit loop
- L1.B evidence-gain gate
- L1.C analysis/history/state
- L1.D helper usage and safety

Level 2 chunks:

- L2.A static reproduction loop
- L2.B five-part working hypothesis
- L2.C candidate/feedback/repair policy
- L2.D learned reproduction lessons
- L2.E helper usage and safety

Helper scripts are also update units. The initial packet intentionally keeps only five helpers: Level 1 candidate diff, submit preflight, submit history; Level 2 candidate plan and issue/code alignment. It does not include fixed initial-spec, analysis-schema gate, or runtime instrumentation helpers. Curator must decide ACCEPT/MERGE/MODIFY/SKIP per chunk/helper and cite train evidence.


## Test-time runtime policy

Level 2 is primarily static at test time. It may use ordinary execution output if naturally available, but the initial static skill packet must not require custom debugger instrumentation, prepared binaries, or GT reachability feedback. Reasoning and reachability diagnostics are training materials for skill distillation, not frozen-test runtime inputs.

## Observation Packager contract

For each sample, output Markdown containing only raw/cropped evidence: sample id, provenance, issue description, trajectory excerpts, submit history, submitted PoCs, submitted `analysis.json`, reasoning diagnostics, reachability diagnostics, final outcome when available.

It must not classify failure modes, infer low-information submits, assign ADD/MERGE/SKIP, invent vulnerability type, or convert evidence into brittle predicate schemas.

## Teacher / Evolver / Patcher / Audit contracts

Teacher output is a proposal, not an automatically merged patch. Evolver/Curator decides chunk-by-chunk using actual `SKILL.md` chunk IDs and helper scripts. Patcher applies minimal changes. Audit blocks GT leakage, sample-specific rules, test evidence, forced submit spam, Level 1/Level 2 conflicts, unsafe helpers, and generic non-actionable lessons.

## Implementation sequence

1. Keep this plan as source of truth.
2. Package train observations.
3. Build Teacher shard prompts.
4. Scaffold full initial two-layer skill packet.
5. After user confirmation, run TraeCLI GPT-5.5 Teacher, global synthesis, Curator, Patcher, Audit.
6. Freeze static skill packet.
7. Evaluate baseline vs skill packet on the 200-sample test set.

## Non-goals

- No online evolution during test.
- No per-sample test-time skill editing.
- No dev loop.
- No automatic acceptance solely because process diagnostics improve.
