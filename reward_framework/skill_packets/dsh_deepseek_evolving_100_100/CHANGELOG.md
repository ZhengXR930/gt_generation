# Skill Packet Changelog

- Preserved the evidence-backed base loop and parser-visible encoding check.
- Merged R.B#1/R.B#2/S.C#2 finalization rules into one state machine.
- Merged the candidate-trace warnings into one raw-input rule.
- Added an explicit FALSE_POSITIVE state: do not finalize on a nonzero exit;
  require one focused revision when a source-backed revision is available.
- Bounded runner-error retries and clean-result revisions to avoid both
  duplicate submission and premature stopping.
- Added TRACE_CONFLICT for target flags that contradict required earlier
  reachability checkpoints.
- Added NO_PROGRESS and ANALYSIS_STALL guards to stop iteration-cap loops,
  unrelated harness probes, and broad source/environment wandering.
- Added a low-specificity issue gate: sanitizer/fuzzer class alone cannot
  justify a candidate or target match without a source mechanism.
