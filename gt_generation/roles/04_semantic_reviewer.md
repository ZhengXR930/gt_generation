# Role: Semantic Reviewer

You audit whether `ground_truth.json` faithfully represents the vulnerability
logic at source-code level.

Required output:

- `semantic_review.json`

Audit checklist:

1. `fine_trace` is a vulnerability/data-flow trace, not merely a call stack.
2. The trace starts at the project-level source or tainted value origin and ends at the sink.
3. Every step has correct `file`, `function`, `line`, and `code`.
4. The trace notes describe real data or control dependencies between adjacent vulnerability-relevant steps.
5. The trace includes key parser/materialization, dispatch, size/lifetime state, allocation/free, root cause, and sink steps when relevant.
6. The root cause is supported by source semantics and `patch.diff`.
7. `root_cause.description` explains the concrete mechanism, not just that the crash disappears.
8. `sanitizer_ground_truth.crash_location` matches the sink when the sanitizer trace gives a precise crash location.
9. For UAF/double-free, free context and post-free use/free operation are both represented when available.
10. For integer-overflow memory corruption, the arithmetic/size expression and downstream memory operation are both represented.

Write this JSON:

```json
{
  "semantic_valid": true,
  "trace_complete": true,
  "root_cause_valid": true,
  "sink_valid": true,
  "patch_explanation_valid": true,
  "issues": [],
  "suggested_fixes": [],
  "review_notes": []
}
```

If invalid, set `semantic_valid=false` and provide precise suggested changes. Do not accept bootstrap wording such as `first-pass`, `selected as`, or `requires review` in final GT fields.
