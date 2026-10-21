# Cleaned Final Skill Packet: Evidence State Machine

This packet is a cleaned successor to the through-batch-009 packet. The
original distillation run was removed after this packet was materialized and
hashed. This version merges overlapping
crash-finalization and candidate-trace rules into one explicit decision policy.

The key behavioral requirement is:

> A nonzero exit is not target success. If the returned crash is not mapped to
> the issue-described mechanism, submit one focused, non-duplicate,
> source-backed revision when one is available; only then switch hypotheses or
> stop as unresolved.

The state machine also treats missing progress as terminal for the active
hypothesis: repeated same-depth attempts, unrelated control probes, and a
target flag that conflicts with required trace checkpoints cannot be promoted
to success.

Only the two skill directories are required by the harness. The helper scripts
are copied unchanged from the original packet.
