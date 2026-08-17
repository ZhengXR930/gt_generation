# GT-Free Reward Framework

This package implements an episode-local reward loop for vulnerability
reproduction. It does not optimize or patch OpenHands. The controller
intervention is a platform-neutral supervisor that observes the trajectory,
requests submission when a runnable candidate appears ready, and gates
unfocused actions while that candidate is being materialized.

Ground truth is never materialized in the Reward Agent view and is not used by
the reward loop.

## Information boundary

The Reward Agent sees only:

- the public issue description;
- the vulnerable source codebase;
- the Subject trajectory recorded during this episode;
- the submitted `analysis.json` for the exact candidate;
- candidate execution and passive runtime facts;
- earlier evidence from the same episode.

It does not see GT fine traces, invariant graphs, known PoCs, historical crash
states, held-out results, dataset sanitizer traces, or evaluation reports.

## Reward Spec

The Reward Spec is generated once per sample from public issue + codebase and
has this fixed shape:

```json
{
  "admission": [],
  "source": [],
  "root": [],
  "propagation": {
    "required": [],
    "optional": []
  },
  "sink": []
}
```

The dimensions are aligned with the deterministic evaluator:

- `admission`: the input is accepted by the real parser/API/driver and
  converted into the issue-relevant internal object.
- `source`: attacker-controlled issue-relevant data enters internal state.
- `root`: the vulnerable state predicate is established.
- `propagation.required`: sparse necessary transitions connecting the state to
  consumption.
- `propagation.optional`: diagnostic transitions that must not gate feedback.
- `sink`: the vulnerable state is consumed by an issue-relevant dangerous
  operation.

Every claim cites source-relative file/function/line. Root and sink claims use
a side-effect-free `check` over source-visible operands. Propagation claims use
`from`, `to`, and `via`, with an optional `check`.

## Candidate submission

The Subject Agent should materialize every runnable candidate at the standard
workspace paths:

- `/workspace/poc.bin`
- `/workspace/analysis.json`

The controller automatically submits each new PoC+analysis bundle exactly once.
The Subject may also submit explicitly through the first-class tool:

```json
{"poc_path": "/workspace/poc.bin", "analysis_path": "/workspace/analysis.json"}
```

`analysis.json` must be the same artifact used by formal evaluation:

```json
{
  "sample_id": "...",
  "fine_trace": [],
  "vuln_logic": {}
}
```

The framework rejects incomplete causal analyses at submission time. A valid
analysis must bind the source, root-cause, sink, and propagation claims to
role-marked `fine_trace` steps, including operands and required relations for
root and sink. It must also include `vuln_logic.issue_alignment` comparing the
candidate's admission, source, root-cause, propagation, and sink claims against
the public issue description. The framework checkpoints every valid submission,
content-deduplicates PoCs, runs the candidate, records runtime facts, and
returns factual feedback if the trigger oracle did not fire.

## Supervisor

The supervisor is not a harness optimizer and does not modify OpenHands. Its
high-level decisions are:

- `continue`
- `request_submission`

If the workspace already contains a new `poc.bin` plus `analysis.json`, the
harness submits it directly before spending another Subject model turn. If the
trajectory indicates a concrete runnable hypothesis but artifacts are absent,
the request becomes a candidate-materialization checkpoint. The Subject may
still inspect exact local source lines, confirm the local harness interface,
write artifacts, and run local sanity checks. Broad exploration, external
browsing/downloads, legacy `submit.sh` use, and unrelated searches are blocked
until the current candidate is materialized or submitted. The reminder and gate
do not include vulnerability advice.

## Feedback

Feedback reports:

- the longest confirmed stage prefix;
- the first unresolved boundary;
- trusted contradictions, if any;
- stage-status change from the previous distinct candidate;
- whether the independent runtime trigger oracle fired.

Feedback is non-prescriptive. It must not suggest bytes, field values, commands,
patches, next steps, or a complete PoC.

## OpenHands usage

The normal evaluation runner remains under `poc_generation/` and only launches
the baseline OpenHands evaluator. Reward mode is selected only by a
reward-framework-owned launcher or environment that points OpenHands at
`reward_framework.openhands_entrypoint`.

Isolation rules:

- `external/OpenHands` remains the pinned pristine checkout for normal
  OpenHands evaluation.
- `poc_generation` does not select the reward entrypoint, create
  `.reward_framework`, rewrite `README.md`/`submit.sh`, or persist reward
  framework state in normal results.
- The reward entrypoint refuses to run unless a reward-owned launcher selects
  the reward profile and sets `OPENHANDS_REWARD_FRAMEWORK=1`.
- Reward workspace changes, native submit tooling, feedback, and supervisor
  gates must stay inside `reward_framework`.

The current OpenHands evaluation flow writes one top-level
`poc_generation/poc_results/<model>/<sample>/analysis.json`. When a PoC is
submitted, the matching per-submission `analysis.json` is also saved and
reachability can be executed immediately, avoiding a second Docker pull.
