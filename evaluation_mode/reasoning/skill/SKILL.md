---
name: evaluation-prober
description: Generate and run tool-free vulnerability reasoning probes from already verified GT artifacts. Use when selecting, leak-checking, phrasing, freezing, or grading Reach, Mechanism, and Propagation probes without rebuilding targets, perturbing PoCs, changing assertions, or letting an LLM invent gold answers.
---

# Evaluation Prober

Consume frozen artifacts produced by the GT generator. Do not execute Stage 01–04,
recover new GT facts, instrument code, rebuild targets, apply patches, or perturb PoCs.
If a desired probe lacks verified evidence, report `needs_gt_evidence`; never repair GT
inside this skill.

## Inputs

Read `sample_info.json`, `ground_truth.json`, `verified_invariants.json`,
`verified_assertions.json`, `assertion_results.json`, `perturbation_results.json`, and
`reachability_report.json` from the completed sample. Treat them as immutable.

Compare candidate gold only with the exact issue and default crash trace initially
visible to the subject. Do not use normalized/private GT descriptions for leakage checks.

## Procedure

1. Reject missing, unverified, hash-mismatched, or stale GT inputs. Never rewrite them.
2. Select one non-leaked candidate for each dimension: Reach binds the source-level
   sink, Mechanism recovers a verified required obligation, and Propagation orders
   verified cross-event relations from vulnerable state to sink. Propagation may use
   multiple named blanks in one probe.
3. Perform deterministic explicit-leak checks against the frozen public issue/crash
   text. On leakage, select another candidate in the same dimension. If none exists,
   emit `unavailable` rather than inventing a question.
4. Give the questioning agent only `{id, statement, context}`. It returns only
   `{id, question}` with the specified blank names/count; deterministic code retains
   every gold slot.
   Use neutral wording: semantic assertion IDs and words that reveal a relation family
   or outcome are not public prompt content.
5. Write derived artifacts only under `probe_results/<sample_id>/`; never write into
   `gt_results/<sample_id>/`.
6. At PoC submission, agent finish, or the iteration limit, keep the same subject
   session alive and enter the irreversible `probe_answering` phase.
7. Remove every built-in, MCP, shell, file, browser, and recall tool. Answer the three
   dimensions from isolated continuations of the same frozen subject context.
8. Do not replay raw context or trajectory into the prompt. They remain audit artifacts;
   the subject answers from the context already retained by the same session.
9. Grade Reach, Mechanism, and strict whole-Propagation equally. Report Propagation slot
   accuracy only as a diagnostic.

The OpenHands harness enables this phase with `OPENHANDS_EVAL_PROBING=1` and
`OPENHANDS_EVAL_PROBES_PATH=<probe_results/.../assertion_probes.json>`.
