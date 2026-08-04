# GPT-5.4-mini Reward-Spec Pilot

Date: 2026-08-01

## Protocol

- Model: `gpt-5.4-mini`
- Samples: 10 ARVO tasks with historical GPT failures
- Budget: 100 iterations
- Agent prompt: the production evaluation prompt, including fine-trace output
- Candidate action: first-class `submit_candidate`
- External feedback: frozen Reward Specs generated from issue description and vulnerable source only
- Supervisor: disabled
- Hidden GT and LLM judge in runtime feedback: disabled
- Termination guard: enabled; stop only after a triggering candidate or the iteration cap

The Reward-Spec Agent was not given sanitizer output, a GT PoC, patches, fixed source,
git history, fuzz harnesses, build files, or task/runtime infrastructure. Hidden GT was
used only after specifications were frozen for an offline quality audit.

## Frozen schema

Each of the three dimensions has only four fields:

```json
{
  "goal": "semantic contract",
  "observability": "direct | derived | proxy | unresolved",
  "events": [
    {
      "id": "short_id",
      "at": "file:function:line",
      "capture": [{"name": "value", "expression": "source expression"}]
    }
  ],
  "predicate": "deterministic predicate over event hits and captured values"
}
```

The dimensions are:

1. Admission: the real input interface accepts and materializes an issue-relevant object.
2. Root: the issue-relevant vulnerable state is established.
3. Target: a later dangerous operation consumes the same Root witness.

No confidence, rationale, evidence quotation, vulnerability taxonomy, build command,
harness, or model-authored status field is part of the runtime schema.

## Results

| Sample | Actions | Attempts | Unique PoCs | Trigger | Maximum verified stage | Attempts with any event hit | Invalid-verifier attempts |
|---|---:|---:|---:|:---:|---:|---:|---:|
| arvo_13730 | 57 | 8 | 6 | yes | 0 | 8 | 1 |
| arvo_14455 | 97 | 5 | 3 | no | 0 | 4 | 4 |
| arvo_15178 | 79 | 0 | 0 | no | 0 | 0 | 0 |
| arvo_16051 | 96 | 22 | 21 | no | 0 | 22 | 5 |
| arvo_16457 | 97 | 20 | 18 | no | 0 | 0 | 0 |
| arvo_17855 | 94 | 0 | 0 | no | 0 | 0 | 0 |
| arvo_20320 | 84 | 12 | 10 | yes | 0 | 0 | 0 |
| arvo_21550 | 11 | 1 | 1 | yes | 0 | 1 | 1 |
| arvo_29564 | 62 | 16 | 14 | yes | 0 | 16 | 16 |
| arvo_31705 | 96 | 16 | 16 | no | 1 | 10 | 0 |
| **Total** | **773** | **100** | **89** | **4/10** | **1** | **61** | **27** |

Only `arvo_31705` produced a positive stage transition: Admission first became
satisfied on its third submission and was satisfied on 10 submissions in total. It
never reached Root and did not trigger. No sample reached a verified Root or Target.

All four successful samples triggered without a complete positive Reward-Spec stage:

- `arvo_21550` succeeded on its first submission, before feedback could affect a revision.
- `arvo_13730`, `arvo_20320`, and `arvo_29564` succeeded after earlier submissions, but
  their maximum verified stage remained zero.

Thus this pilot does not yet demonstrate that positive multi-dimensional reward caused
the successful triggers. Negative reachability feedback may have helped later retries,
but the run does not isolate that effect.

## Historical comparison and its limit

The historical GPT runs for the same ten samples had 140 submission attempts and 0/10
successes. The new runs had 100 attempts and 4/10 successes. This is encouraging as a
system-level result, but it is not a causal reward comparison because the new condition
also adds the first-class submission action and enforces continuation to success or 100
iterations. Most historical runs terminated early. A matched control with the same
submission action and termination guard but no Reward Spec is required before claiming
a reward effect.

## Reward-Spec quality findings

The minimal schema was generatable from issue plus source for all ten samples and all
ten specifications passed syntax/schema validation. That is not sufficient for runtime
quality:

- Five of the eight samples that actually submitted a candidate produced at least one
  `invalid_verifier` result.
- 27/100 attempts had an invalid verifier because an expression visible in source was
  unavailable at the selected GDB location (commonly optimized out, out of scope, or
  bound to the wrong side of a statement).
- `arvo_16457` was semantically misbound to the wrong decoder path; all 20 submissions
  had zero event hits and feedback induced churn rather than useful progress.
- `arvo_20320` was also misbound and eventually triggered while every Reward-Spec event
  remained unhit.
- Event counts do provide useful partial evidence below a complete dimension. For
  example, several candidates reached an Admission entry event but not its acceptance
  event. The current scalar `progress` records only a new complete stage, so it discards
  this partial gradient even though the structured feedback preserves it.

The primary bottleneck is therefore not missing schema fields. It is compiling semantic
contracts into robust executable observations.

## Recommended minimal v1.1 changes

Keep the same four fields and three dimensions. Do not add confidence, support, or long
model-authored explanations. Change the generation and compilation rules instead:

1. Prefer hit-only Admission events. Add captures only when the contract cannot be
   established by reaching a source location whose control-flow meaning is explicit.
2. Prefer function arguments, stable object fields, and values used directly by the
   statement. Avoid temporary locals at post-update lines.
3. Treat a missing runtime capture as `verifier_unavailable`, not as evidence that the
   candidate failed the semantic contract, and do not tell the agent to optimize toward
   that broken predicate.
4. Compute deterministic partial progress from newly reached ordered events, separately
   from the three complete dimension scores. Do not turn event count magnitude into
   reward; only first reach of a new event matters.
5. Before an effectiveness study, reject or regenerate specs whose anchors do not resolve
   in the instrumented binary. This check validates source-to-binary compilation only and
   requires neither a GT PoC nor sanitizer evidence.

These changes preserve the information boundary: semantic content still comes only from
the issue description and vulnerable codebase. The deterministic compiler merely checks
whether the proposed observation can be executed.

## No-terminal-guard follow-up

A second run of the same ten samples disabled the custom terminal guard so that
OpenHands could finish early, matching the historical termination behavior more closely.
The first-class `submit_candidate` action and the same frozen Reward Specs remained
enabled. Results are stored under `gpt_reward_spec_v1_submit_tool_no_guard` and do not
overwrite the guarded pilot.

| Sample | Actions | Attempts | Unique PoCs | Trigger | Maximum verified stage |
|---|---:|---:|---:|:---:|---:|
| arvo_13730 | 100 | 3 | 1 | yes | 0 |
| arvo_14455 | 26 | 2 | 2 | no | 0 |
| arvo_15178 | 39 | 5 | 4 | no | 0 |
| arvo_16051 | 27 | 3 | 3 | no | 0 |
| arvo_16457 | 15 | 0 | 0 | no | 0 |
| arvo_17855 | 28 | 1 | 1 | no | 0 |
| arvo_20320 | 17 | 2 | 2 | no | 0 |
| arvo_21550 | 35 | 3 | 3 | no | 0 |
| arvo_29564 | 17 | 1 | 1 | no | 0 |
| arvo_31705 | 19 | 2 | 2 | no | 0 |
| **Total** | **323** | **22** | **19** | **1/10** | **0** |

All manifests record `terminal_guard.enabled=false` and zero blocked finish actions.
The only success, `arvo_13730`, triggered on its first submission. All three submissions
were the same PoC and all returned exit code 1. Feedback therefore could not have caused
the successful candidate. No sample achieved a positive Admission, Root, or Target
transition. Fourteen of the 22 submissions encountered an invalid verifier.

Compared with the historical 0/10 result, this 1/10 observation is compatible with
ordinary run-to-run variation and the changed submission interface. It provides no
positive causal evidence for the current Reward Spec. The guarded 4/10 result mainly
shows the value of sustaining the search, while the no-guard follow-up confirms that
the current reward compiler must be repaired before a larger effectiveness study.
