# Three-dimensional Reward Spec schema study

## Input isolation

Sixteen specs were frozen using only each public issue description and the
vulnerable project's source code. The prompt prohibited sanitizer traces, GT,
saved PoCs, patches/fixed code, git history, build files, fuzz/benchmark
harnesses, submission scripts, task README files, runtime configuration,
sibling samples, and network access. One pilot run that enumerated excluded
file names was discarded and rerun from the project source root. Hidden GT was
consulted only after all candidate specs had been generated.

## Sample strata

- Rich causal narratives: `arvo_17855`, `arvo_29564`.
- Concise causal/property descriptions: `arvo_31301`, `arvo_14455`.
- Semantic property violations: `arvo_17171`, `arvo_23153`.
- Sparse descriptions: `arvo_3325`, `arvo_16457`.
- Concrete input with little causal prose: `arvo_31332`.
- OSS-Fuzz crash-stack reports: `arvo_13730`, `arvo_15178`, `arvo_16051`,
  `arvo_20320`, `arvo_21550`, `arvo_25530`, `arvo_31705`.

## What the research schema produced

| Dimension | Direct | Derived | Proxy | Unresolved |
|---|---:|---:|---:|---:|
| Admission | 7 | 9 | 0 | 0 |
| Root | 10 | 5 | 1 | 0 |
| Target | 0 | 16 | 0 | 0 |

Target is always derived because a valid Target must relate a later dangerous
operation to values captured by Root. This is useful evidence that Root and
Target should remain separate while a full propagation path is unnecessary.

The absence of `unresolved` outputs is not evidence that all specs were good.
The model filled every field even when the issue did not uniquely identify the
relevant source path.

## Post-generation hidden-GT audit

This is a qualitative schema audit, not an effectiveness measurement.

- Strongly aligned issue/source bindings (9): `arvo_13730`, `arvo_14455`,
  `arvo_16051`, `arvo_17171`, `arvo_17855`, `arvo_21550`, `arvo_23153`,
  `arvo_29564`, `arvo_31705`.
- Correct vulnerability pattern or dangerous target but over-specific/wrong
  variant (3): `arvo_15178` selected `blocks` rather than the witnessed `edges`;
  `arvo_25530` found the decode overrun but not the actual allocation-size
  relation; `arvo_31301` found the zero-length hash consumer but bound Root to
  one over-specific caller path.
- Misbound to a different plausible path (4): `arvo_16457`, `arvo_20320`,
  `arvo_31332`, `arvo_3325`.

The four misbindings are especially important: their source locations and
predicates are executable and locally coherent. Executability and model-rated
confidence therefore cannot establish that a reward is relevant to the public
issue.

## Schema decision

The executable core needs five fields per dimension:

1. `claim`: what the dimension means semantically.
2. `support`: whether the issue uniquely supports the source binding, including
   exact issue excerpts.
3. `observability`: direct, derived, proxy, or unresolved.
4. `events`: minimal source observations and side-effect-free captures.
5. `predicate`: deterministic relation over the observations.

`confidence` is removed from the formal schema. In this study, incorrect source
hypotheses were still labelled medium/high confidence. Confidence should instead
be derived mechanically from `support` and `observability` and used only for
audit, never to weight candidate rewards.

Detailed free-form `limitations` remains useful in schema-development logs but
is removed from the runtime schema. Its machine-relevant content is represented
by `support.status` and `observability`.

## Eligibility rules

A dimension can return `satisfied` only when:

- `support.status` is `issue_explicit` or `source_disambiguated`;
- `observability` is `direct` or `derived`;
- all referenced events/captures resolve in the vulnerable source build; and
- the deterministic predicate evaluates true over ordered runtime observations.

`ambiguous` or `proxy` may return `proxy_observed` as diagnostic feedback, but
cannot advance the verified reward stage. `unsupported` requires unresolved
observability and an empty verifier.

Target additionally must reference at least one Root capture and enforce that
the consuming event occurs after the matching Root witness. The runtime engine
must retain multiple hits and evaluate predicates existentially over ordered
event tuples; recording only the first breakpoint hit is insufficient for
loops, repeated parser objects, and multi-submission inputs.

## Final simplification before the reward experiment

A focused counter-test added `support.status` and exact issue excerpts to the
formal schema. It correctly made `arvo_3325` unresolved, but still labelled the
wrong `arvo_16457` Rice-decoder hypothesis as `source_disambiguated`. Thus the
extra field did not reliably solve semantic ambiguity and imposed another LLM
self-assessment step.

The runtime schema used for the next effectiveness pilot therefore contains
only `goal`, `events`, `predicate`, and `observability` for each of Admission,
Root, and Target. Issue excerpts, generation logs, and hidden-GT audits remain
separate research records. The simplified schema accepts the information upper
bound of sparse issues instead of presenting an uncalibrated trust label as a
solution.
