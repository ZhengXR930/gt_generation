# Level 1: Submission / Verification Skill

<!-- block-id: L1.A-submit-loop -->
## L1.A Submission loop

Purpose: make each sample spend its limited iteration budget on evidence-bearing validation rather than zero-submit failure, one weak submit then finish, or duplicate/near-duplicate submit spam.

Default loop:

1. Build or revise a concrete candidate.
2. Analyze why the candidate matches the issue/code evidence.
3. Compare with prior submits.
4. Write a pre-submit note that states the new evidence or changed hypothesis.
5. Submit only if the attempt has evidence value.
6. Record the submit and use it to choose the next attempt.

Attempt policy: submit whenever a candidate constitutes a meaningful new test of the current hypothesis; submission count itself is not an objective. If no plausible candidate exists, spend effort understanding code and constructing one. If the candidate is clearly successful, finalize instead of burning attempts.

Update rule: edit this chunk only for recurring failures in submission cadence, premature finish, missing validation, or near-duplicate submit spam.

<!-- block-id: L1.B-evidence-gain-gate -->
## L1.B Evidence-gain gate

A submit is worth an attempt if at least one condition holds:

- it tests a materially different parser/admission hypothesis;
- it tests a materially different source/root-cause/sink/trigger hypothesis;
- it changes candidate family for a reason tied to issue/code evidence;
- it preserves a working parser/source path and focuses on deeper root-cause or trigger repair;
- it submits a concrete candidate after improving the analysis that explains why this candidate should reproduce the issue.

Before submitting, write a note with candidate goal, evidence since previous submit, what changed from previous candidate, expected parser/source/root-cause/sink/trigger behavior, and why this attempt is worth spending.

Exact duplicates should be blocked deterministically by the workspace wrapper when possible. Near-duplicates should produce warnings and structural diff context, but semantic evidence gain should not be decided by keyword heuristics. Teacher later judges submission quality from trajectory, submit history, reasoning diagnostics, and reachability diagnostics.

Update rule: this is the main chunk for lessons about low-information submits, duplicate/near-duplicate submits, or under-exploration.

<!-- block-id: L1.C-analysis-history-state -->
## L1.C Analysis and submit state

Submit after analysis, not as blind file upload. The analysis does not need to pass a rigid schema gate during exploration; checkpoints can be repaired into final `analysis.json` later.

Maintain workspace-local submit state in `.gt_skill_state/submit_history.jsonl`. Use it to compare the current candidate against previous candidates and to avoid losing useful evidence across iterations.

Preserve previously working admission/parser structure when repairing later root-cause or trigger hypotheses. State which parts must remain stable and which one dimension is being repaired.

Update rule: this chunk is for analysis/history/state behavior. Vulnerability-specific reproduction lessons belong in Level 2.

<!-- block-id: L1.D-helper-safety -->
## L1.D Helper usage and safety

Use helpers when available:

```bash
python3 helpers/candidate_diff.py --current <poc> --history-jsonl .gt_skill_state/submit_history.jsonl --out .gt_skill_state/candidate_diff.json
python3 helpers/submit_preflight.py --candidate <poc> --analysis analysis.json --note-file <note.md> --evidence-file <analysis_or_notes.md> --out .gt_skill_state/preflight.json
python3 helpers/submit_history.py record --candidate <poc> --analysis analysis.json --preflight-report .gt_skill_state/preflight.json --note <short-note>
```

Helpers are deterministic and workspace-local. `submit_preflight.py` should perform structural comparison, block exact duplicates, and warn on near-duplicates. It must not claim to determine semantic evidence gain from keywords or note length. Teacher owns semantic submission-quality diagnosis. The helper must not block merely because GT-style reachability is unknown or because `analysis.json` is not pretty.

Safety constraints: do not use sample ids, CVE ids, benchmark-specific paths, GT trace constants, or test evidence. Do not force meaningless submits.

Update rule: helper command changes must match actual helper scripts. Safety constraints should be preserved unless the Curator explicitly strengthens them.
