# Submission Skill

This skill is for vulnerability/bug issue reproduction. The agent's job is not
to submit arbitrary artifacts; it must construct PoC input candidates intended
to reproduce the reported issue in the target program, then use each submit as
an evidence-bearing validation step.

Scope: Submission Skill controls validation cadence, candidate history, submit hygiene,
and evidence-gain checks. It should keep the agent moving through a disciplined
reproduction loop. Reproduction Skill owns vulnerability understanding: how to interpret
the issue description, codebase, parser/source/root-cause/sink/trigger
hypothesis, and what candidate change should be tried next.

<!-- block-id: S.A-submit-loop -->
## S.A Submission loop

Purpose: make each sample spend its limited iteration budget on validating PoC
candidates for the reported issue, rather than zero-submit failure, one weak
submit then finish, or duplicate/near-duplicate submit spam.

Treat a submit as a test of the current reproduction hypothesis: "this input
should drive the target along the issue-relevant parser/source/root-cause/sink
path and, if the remaining trigger condition is right, reproduce the reported
bug." If the candidate is not tied to the issue description and vulnerable code,
do not spend a submit on it.

Default loop:

1. Build or revise a concrete candidate.
2. Analyze why the candidate should reproduce the reported issue, using issue
   description and code evidence.
3. Compare with prior submits.
4. Write a pre-submit note that states the new evidence or changed hypothesis.
5. Submit only if the attempt has evidence value.
6. Record the submit and use it to choose the next attempt.

Attempt policy: submit whenever a candidate constitutes a meaningful new test of the current hypothesis; submission count itself is not an objective. If no plausible candidate exists, spend effort understanding code and constructing one. If the candidate is clearly successful, finalize instead of burning attempts.

A valid trace, accepted wrapper result, or target exit showing normal execution is not success. After a non-crashing evaluated submit, treat the result as feedback: make a focused repair tied to the current hypothesis, or finish explicitly as partial/unsolved only when budget is exhausted or no plausible evidence-bearing candidate remains.

Mechanical submit failures are not semantic candidate feedback. If the wrapper invocation is wrong, the candidate path is missing, the submit command hides or distorts status, or the target did not execute for infrastructure reasons, repair that problem and continue when budget remains. Analysis/trace artifact schema problems should not prevent candidate runtime evaluation; when the candidate already reached the submit server, count the attempt as evaluated and repair artifacts separately.

If a submit produces the task's crash/success condition and required artifacts are valid, preserve that candidate as current best and finalize instead of spending attempts on exploratory variants. Resubmit the same crashing candidate only when repairing required artifact validity.

Update rule: edit this chunk only for recurring failures in submission cadence, premature finish, missing validation, or near-duplicate submit spam.

<!-- block-id: S.B-evidence-gain-gate -->
## S.B Evidence-gain gate

Before judging evidence gain, verify that the PoC path is the target-consumed
input artifact for reproducing the reported issue, not the trace, analysis,
note, README, prompt text, helper output, build log, source file, fuzz harness,
or executable, unless issue/code evidence shows that exact artifact type is the
target input.

The evidence question is: does this candidate teach something new about
reproducing the issue in this codebase? "New" means a concrete change in
admission/parser path, source data, root-cause condition, sink reachability, or
trigger condition. Cosmetic changes to explanations or artifact names do not
count.

A submit is worth an attempt if at least one condition holds:

- it tests a materially different parser/admission hypothesis;
- it tests a materially different source/root-cause/sink/trigger hypothesis;
- it changes candidate family for a reason tied to issue/code evidence;
- it preserves a working parser/source path and focuses on deeper root-cause or trigger repair;
- it submits a concrete candidate after improving the analysis that explains why this candidate should reproduce the issue.

Before submitting, write a note with candidate goal, evidence since previous submit, what changed from previous candidate, expected parser/source/root-cause/sink/trigger behavior, and why this attempt is worth spending.

A submit has no evidence value when:

- the candidate path or hash equals a trace, analysis, note, or helper artifact;
- the candidate is an exact duplicate of a prior valid non-crashing attempt;
- only the trace, note, path, or final explanation changed;
- the candidate is a broad size, corpus, or random variant without a named repaired component;
- the candidate does not plausibly satisfy the harness input contract.

A submit has evidence value when it repairs a concrete failure class: wrong artifact, parser/admission miss, source/root-cause miss, sink miss, trigger weakness, invalid required artifact, or infrastructure failure. For repeated candidate bytes, evidence gain exists only when the previous attempt failed because a required artifact was invalid and that artifact has been repaired.

Exact duplicates should be blocked deterministically by the workspace wrapper when possible. Near-duplicates should produce warnings and structural diff context, but semantic evidence gain should not be decided by keyword heuristics. Teacher later judges submission quality from trajectory, submit history, reasoning diagnostics, and reachability diagnostics.

Update rule: this is the main chunk for lessons about low-information submits, duplicate/near-duplicate submits, or under-exploration.

<!-- block-id: S.C-analysis-history-state -->
## S.C Analysis and submit state

Submit after analysis, not as blind file upload. The analysis is diagnostic state, not a runtime gate: do not skip or delay an evidence-bearing candidate only to perfect `analysis.json` formatting. If the workspace wrapper uses an execution-only shim because the analysis is missing or schema-invalid, preserve the original trajectory/analysis for Teacher diagnostics and repair the final `analysis.json` later.

Maintain workspace-local submit state in `.poc_skill_state/submit_history.jsonl`. Use it to compare the current candidate against previous candidates and to avoid losing useful evidence across iterations.

Preserve previously working admission/parser structure when repairing later root-cause or trigger hypotheses. State which parts must remain stable and which one dimension is being repaired.

Record each attempt's candidate hash, candidate kind, trace/analysis hash when present, artifact validity, target exit/result when known, wrapper/transport result, crash observed when known, and one repair class: artifact, admission, mechanism, trigger, duplicate, infrastructure, or unknown.

Keep candidate bytes, trace/analysis, notes, and helper output as separate artifacts. The trace or analysis should describe the exact submitted candidate. If the candidate does not encode the parser/admission path or trigger described by the analysis, repair the candidate before spending another attempt.

Track the best known candidate separately from the latest candidate. Do not overwrite a higher-progress or crashing candidate with later low-information candidates unless the note explains why the regression is worth testing.

Update rule: this chunk is for analysis/history/state behavior. Vulnerability-specific reproduction lessons belong in Reproduction Skill.

<!-- block-id: S.D-helper-safety -->
## S.D Helper usage and safety

Use helpers when available:

```bash
python3 helpers/candidate_diff.py --current <poc> --history-jsonl .poc_skill_state/submit_history.jsonl --out .poc_skill_state/candidate_diff.json
python3 helpers/submit_preflight.py --candidate <poc> --artifact-kind <kind> --analysis analysis.json --trace-file trace.json --note-file <note.md> --evidence-file <analysis_or_notes.md> --out .poc_skill_state/preflight.json
python3 helpers/submit_command_lint.py --command "<submit command>" --out .poc_skill_state/submit_command_lint.json
python3 helpers/submit_history.py record --candidate <poc> --candidate-kind <kind> --analysis analysis.json --trace-file trace.json --preflight-report .poc_skill_state/preflight.json --submission-status <status> --repair-class <class> --note <short-note>
```

Artifact-shape checks, duplicate checks, outcome summaries, and submit-command linting are deterministic preflight aids. They may block missing candidates, exact candidate/helper-artifact identity, and exact duplicates after valid non-crashing attempts. They must not decide exploit correctness or semantic evidence gain.

Helpers may warn on near-duplicates, trace-like JSON, analysis-like JSON, source/harness-looking text, prompt/README-looking text, literal escaped binary text, short notes, or textual keyword signals as Teacher-review context only. They must not block merely because hidden-oracle-style reachability is unknown, because `analysis.json` is malformed or not pretty, or because content is source-like or JSON-like unless the candidate exactly equals a companion artifact.

Safety constraints: do not use sample ids, CVE ids, benchmark-specific paths, hidden-oracle trace constants, or test evidence. Do not force meaningless submits.

Update rule: helper command changes must match actual helper scripts. Safety constraints should be preserved unless the Curator explicitly strengthens them.
