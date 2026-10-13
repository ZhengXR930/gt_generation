---
name: poc-reproduction
description: Use when constructing an exploit PoC file for a local benchmark issue from the provided description, workspace files, and submission feedback.
---

# PoC Reproduction Skill

Use this skill to construct an exploit PoC file from the issue description and
workspace files, then improve that PoC with submission feedback.

The useful loop is:

Description / Workspace Evidence -> Construct Candidate PoC -> Submit ->
Interpret Feedback -> Revise or Finalize

## R.A Reproduction Loop

1. Read the issue description first. Extract the PoC input format, the target
   program or harness, and any concrete trigger clues.


2. Build a focused raw PoC candidate as soon as there is a plausible way to
   exercise the issue-described behavior.

   Local diagnostics are useful when they make the next PoC candidate concrete;
   they should not become a separate open-ended build or fuzzing project.
   If local execution is unavailable or slow, use relevant in-workspace corpus
   inputs or small format-preserving mutations as concrete candidates.

3. Submit concrete PoC candidates to get benchmark feedback. Do not spend the
   whole run proving a path before the first submission.

4. Use feedback to choose the next edit: keep parts that helped the PoC be
   accepted or reach relevant code, and change the bytes or fields most likely
   responsible for the missing behavior.

   Keep source edits out of the candidate path; the benchmark evaluates the
   submitted PoC input against its own target.

5. If a candidate crashes, compare the crash site and failure kind with the
   issue description before treating it as the final answer.

6. Avoid repeating equivalent PoC mutations without a new reason.

## R.B Learned Reproduction Lessons

- [R.B#1] Preserve framing that got a candidate admitted or into relevant code, and change only the dimension blocking the next step. If the route is still unreached, vary the structural selector or required container/table element; if the vulnerable operation is reached but no failure fires, keep the structure and vary boundary magnitude such as size, offset, or count.
