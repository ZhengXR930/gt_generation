# GT Contract Schemas

These schemas describe the target GT contract for migrated artifacts. They are
intentionally focused on deterministic comparison fields instead of generation
metadata:

- No artifact-level `schema_version` is required.
- `type` is optional. It may preserve an old free-text subtype, but it is not a
  generation requirement and should not be used for scoring.
- Source expressions are carried by `operands`.
- Semantic comparison is carried by `relation`.
- `ground_truth.json` anchors are aligned so source, root cause, and sink all
  have explicit source operands; root cause and sink also require a relation.
- `verified_invariants.json` nodes and edges both require `relation`, making the
  invariant graph directly comparable with semantic claims.

Suggested migration order:

1. Upgrade `ground_truth.json` anchors with `operands` and root/sink relations.
2. Materialize `root_cause_criterion` as a `root_cause` node in
   `verified_invariants.json`.
3. Normalize invariant node/edge operands and relations.
4. Validate assertion foreign keys against real invariant node/edge ids.
