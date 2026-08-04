# Runtime Hypothesis Feedback Experiment

This experiment is isolated from the benchmark's frozen GT, evaluator, and
official model result trees.  Its purpose is to test whether execution feedback
about an agent's *own* vulnerability hypothesis improves PoC generation.

## Unified Reward Agent

The current protocol uses one external Reward Agent in two roles.  Before an
episode, `initialize_spec` receives only the closed-book public issue and the
same hydrated vulnerable source tree available to the coding agent.  It may use
three bounded read-only tools (`list_files`, `search_code`, and `read_source`)
and freezes a four-stage Reward Map under `reward_specs/`.  Anchor functions are
validated against the referenced source files; the artifact records hashes of
every source file read.  It cannot access GT, known PoCs, historical sanitizer
traces, patches, commit history, or the network.

After each distinct candidate, the same Reward Agent's `diagnose_submission`
role has no tools and no live source access.  It receives the frozen Reward Map,
the untrusted submitted trace, bounded deterministic runtime evidence, the
authoritative target result, and a deterministic delta from the previous
distinct PoC.  The verifier, rather than the model, owns execution, probes,
typed relations, and evidence normalization.  Diagnosis uses explicit
`confirmed`, `contradicted`, `unresolved`, `not_reached`, `not_declared`, and
`collapsed_with_target` states.  In particular, unavailable Root evidence is
`unresolved`, never evidence that the Root condition is false.

The four semantic stages are Admission, Root, optional Propagation, and Target.
Propagation may be `collapsed_with_target` when the Root operation itself
consumes the vulnerable state, or `not_declared` when the public issue and
source cannot support a distinct transition.  Target remains the only terminal
success oracle.  Feedback reports facts and the first evidence boundary but is
forbidden from suggesting mutations, bytes, commands, patches, or a PoC.

## Candidate-bootstrap state machine

Condition C can optionally use an issue-only LLM monitor to decide when the
agent has learned enough about the executable interface and input format to
construct its first runnable candidate:

```bash
python experiments/runtime_hypothesis_feedback/run_experiment.py \
  --condition c --candidate-monitor --arvo-id 3325 --max-iter 50
```

The monitor receives only the public issue skeleton and a bounded recent event
window. It does not receive GT, propose a PoC, identify source locations, or
judge vulnerability correctness. Its semantic outputs are limited to interface
readiness and analysis stall. A deterministic controller owns the states
`orient -> bootstrap_required -> feedback_loop`, recognizes an actual
`submit.sh` action, and records all decisions in
`candidate_state_machine.jsonl`. All normal tools remain available during the
bootstrap state.

After the LLM declares semantic readiness, lack of an actual submission is no
longer a semantic question. If the agent spends another configured interval on
tools without invoking `submit.sh`, the deterministic controller repeats the
bootstrap instruction without calling the monitor LLM again. This prevents a
monitor from treating useful but unbounded source analysis as a reason to
postpone execution indefinitely, and keeps monitor latency/concurrency out of
the post-readiness control path.

The controller never reads `gt_results/`.  It uses only:

- the public CyberGym issue description and vulnerable source exposed to the
  subject;
- the PoC and candidate trace submitted by the subject;
- execution of that PoC against the vulnerable ARVO image.

## Protocol

Each candidate trace retains the normal GT-shaped JSON-array format.  A step may
add:

```json
{
  "role": "source",
  "captures": {"input_len": "(long)size"},
  "condition": {"op": "gt", "left": "input_len", "right": 63}
}
```

`role` is one of `source`, `propagation`, `root`, or `sink`.  `captures` are
agent-supplied GDB expressions.  `condition` can compare a captured field with
another field or a JSON scalar using `eq`, `ne`, `lt`, `le`, `gt`, or `ge`.
These agent-declared conditions are diagnostic hypotheses: they cannot confirm
the vulnerable state by themselves. The runtime records whether the declared
Root location and condition were observed, while the real target remains the
only success oracle.

Agent-supplied captures are optional. The issue-only external observer may
select at most four passive runtime expressions from verbatim fine-trace text.
Assignments are reduced to their passive right-hand side; calls, literals,
invented expressions, and unsafe syntax are rejected per observation without
discarding valid Admission/Root anchors. Observer probes are retried at function
entry when an exact-line value is optimized out. Parser-loop probes retain the
last of a bounded sample, and pointer dereferences also record their address.
Capture kinds prevent address/scalar conflation; the verifier may derive only
compatible arithmetic facts such as an address delta. These observations are
diagnostic and cannot independently confirm Root.

The feedback proxy forwards the submission unchanged to a separate CyberGym
server, then runs the PoC under GDB with breakpoints proposed by the candidate
trace. Exact line hits and weaker same-file function hits remain distinct. A
function hit is only `location_reached_only`; it never confirms Admission or a
vulnerable Root state. The authoritative Target signal remains the real target
exit/sanitizer result.

The online response is intentionally compact: Admission, Root, Target, the
first unresolved gap, and evidence for that boundary. It contains no repair
instruction. A candidate-declared Root condition is reported explicitly as
satisfied, false, or unresolved, while remaining clearly distinct from a
verifier-owned condition and from target success. When a reached Root has no
runtime condition, the response includes the optional generic
`captures`/`condition` protocol so a later submission can recover the dense
state evidence used by the initial DeepSeek pilot without changing the
production task prompt.

Propagation remains a non-blocking candidate-hypothesis diagnostic: it asks
whether a distinct downstream consumer was observed after Root. It never
becomes `next_gap`, is not GT, and is not proof of vulnerability correctness.
Many memory-safety roots are already the consuming operation, and public crash
stacks are evidence rather than mandatory state-machine transitions. Full
mappings, breakpoint hits, and captures are stored in `feedback_logs/`.
For identical public skeleton and trace input, the external semantic mapping
is cached. Evidence is also
accumulated monotonically per PoC byte hash, so rewriting a trace cannot make a
duplicate candidate appear to gain or lose confirmed progress.

### Reward Agent feedback explanation

Condition C enables the unified Reward Agent with:

```bash
export HYPOTHESIS_LIGHTWEIGHT_REWARD=1
export HYPOTHESIS_REWARD_MODEL=deepseek-chat
bash experiments/runtime_hypothesis_feedback/start_services.sh
```

Use the same exported variables for `run_experiment.py` so the result namespace
and manifest record the treatment. The initialization role uses bounded
read-only source tools once and freezes the task Reward Map. The per-submission
diagnosis role has no tools. It receives the public issue, frozen Reward Map,
submitted trace, bounded runtime evidence, and previous-distinct-candidate
delta. The issue is the trusted task contract and the trace is an untrusted
hypothesis. Stage states, the evidence frontier, delta text, and runtime fact
text are verifier-owned. The diagnosis role can only select IDs from a
deterministic fact catalog and explain their significance. An invented ID or a
changed stage state is rejected; after one constrained correction failure the
system emits a deterministic fallback. The online response retains the deterministic evidence used by
the original DeepSeek pilot: stage status, per-step exact/function hits,
observed locations, captured runtime values, capture failures, condition
results, typed runtime relations, and the authoritative target result. Global
fine-trace order is treated as causal explanation rather than literal line
execution order; temporal order is assessed only for an explicit Root to
downstream-consumer pair using monotonic GDB event sequence. Advice, mutation
directions, payload bytes, commands, source patches, consumer suggestions, and
complete PoCs are forbidden. If the call fails or its JSON is invalid, the
dense deterministic evidence and its neutral error-boundary summary remain.

The experimental runner removes nested `.git` metadata from each hydrated
subject workspace before OpenHands starts. This prevents future fix commits in
the supplied repository history from acting as an answer channel.

## Isolation

- `server/`: independent CyberGym database and submission logs.
- `feedback_logs/`: one feedback record per experimental submission.
- `results/`: experimental OpenHands checkpoints and manifests.
- `runs/`: service and runner logs.

The existing `gt_results/`, `evaluator/`, and
`poc_generation/poc_results/` directories are read-only inputs or untouched.

## Run

Start the isolated validation server and feedback proxy:

```bash
bash experiments/runtime_hypothesis_feedback/start_services.sh
```

Run condition B (structured hypothesis prompt, no runtime feedback):

```bash
/home/xinran/.cache/pypoetry/virtualenvs/openhands-ai-pW2ZHCQY-py3.12/bin/python \
  experiments/runtime_hypothesis_feedback/run_experiment.py \
  --condition b --arvo-id 12420 --max-iter 50
```

Run condition C with the identical prompt and runtime feedback enabled:

```bash
/home/xinran/.cache/pypoetry/virtualenvs/openhands-ai-pW2ZHCQY-py3.12/bin/python \
  experiments/runtime_hypothesis_feedback/run_experiment.py \
  --condition c --arvo-id 12420 --max-iter 50
```
