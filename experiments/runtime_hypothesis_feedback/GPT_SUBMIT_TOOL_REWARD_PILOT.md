# GPT-5.4-mini submit-tool reward pilot

Date: 2026-08-01

## Configuration

- Agent model: `gpt-5.4-mini`
- Agent endpoint: `https://api.zhizengzeng.com/v1`
- Credential source: `OPENAI_API_ZHIZENGZENG` in `config.txt`
- Prompt: the standard evaluation prompt, with only the submission transport changed
  from a shell command to the native `submit_candidate(poc_path, trace_path)` tool
- Runtime reward: enabled
- Trajectory supervisor: disabled
- Hidden GT in feedback: disabled
- Maximum agent iterations: 100

The endpoint was tested with a direct chat-completions request and returned HTTP 200
from `gpt-5.4-mini`.

## Valid pilot results

| Sample | Native tool selections | Valid submissions | Unique PoCs | Crash | Behavior after reward |
|---|---:|---:|---:|---:|---|
| `arvo_15178` | 1 | 1 | 1 | no | Finished immediately after the failed reward |
| `arvo_20320` | 2 | 1 | 1 | no | Continued repository analysis, then finished without a revised submission |
| `arvo_31705` | 2 | 1 | 1 | no | Continued analysis, but produced no second valid submission |
| `arvo_16051` | 0 | 0 | 0 | no | Prepared artifacts but never selected the native submission tool |

All three valid submissions produced a valid trace and a complete, non-GT reward.
Their feedback consistently reported that the target was not triggered and that the
declared issue root was not reached. `arvo_15178` and `arvo_20320` also reported that
the input-format gate was not declared; `arvo_31705` declared a format gate but did
not reach it. The trace-to-issue mapping completed without an error for all three.

Checkpoints and final fine traces were persisted for all four samples. Per-submission
PoC, trace, runtime output, and reward are persisted for every valid submission.

## Interpretation

This pilot validates the mechanism, not an improvement in PoC success rate:

1. GPT can select the portable first-class submission action.
2. A valid submission deterministically returns the intended four reward dimensions:
   format, root, propagation, and target.
3. The agent reads and reasons about the reward.
4. Without a supervisor, the reward does not reliably cause a revised candidate to
   be submitted. None of the three failed submissions became a second valid attempt.

Therefore reward quality and iteration policy are separate variables. The current
evidence says that the reward is informative, but reward alone is insufficient to
close the candidate-revision loop reliably.

The pilot exposed an incorrect termination policy: `max_iter=100` had been treated
as a ceiling while OpenHands still accepted an early `finish` with
`task_completed=partial/false`. The submit-tool adapter now enforces the intended
protocol. It accepts termination only after a structured submission response proves
that the target was triggered, or when the controller actually reaches the configured
iteration limit. Any earlier `finish` is replaced by a generic continuation notice;
it performs no semantic trajectory analysis and supplies no PoC guidance.

Historical GPT result counts must not be used as a direct control here. Those runs
used a different candidate capture/submission mechanism and recorded 26--42 attempts
for these samples, so comparing their raw submission counts with explicit native-tool
calls would confound reward with transport and capture policy.

`arvo_31332` is excluded from the native-tool/reward conclusion: it succeeded during
an earlier pre-enforcement run that invoked the shell submission path directly, and
the issue text exposed an exact triggering input.

## Next valid experiment

To estimate the causal effect of reward, keep the model, prompt, native tool, sample,
seed policy, and budget fixed, and vary only the tool response:

- execution-only response: exit code and sanitizer output;
- structured reward response: format/root/propagation/target.

No supervisor should be used in either arm. The primary metric should be whether a
failed first submission is followed by a distinct second PoC that advances a reward
stage or triggers the target. This avoids treating mere extra submissions as progress.

## Corrected terminal-policy rerun

After enforcing success-or-global-limit termination, the same four samples produced:

| Sample | Endpoint | Controller behavior | Attempts | Unique PoCs | Result |
|---|---|---|---:|---:|---|
| `arvo_15178` | target trigger | automatic success termination | 2 | 1 | success |
| `arvo_16051` | target trigger | automatic success termination | 1 | 1 | success |
| `arvo_20320` | 100-iteration limit | 4 premature finishes blocked | 18 | 17 | no trigger |
| `arvo_31705` | 100-iteration limit | 13 premature finishes blocked | 0 | 0 | no trigger |

`arvo_15178` demonstrates format-reward repair, not PoC-content repair: the same PoC
already crashed on the first attempt, but its malformed trace was rejected; GPT fixed
the trace and resubmitted it. `arvo_16051` succeeded on its first valid submission.

`arvo_20320` is the strongest content-reward diagnostic. The guard converted four
would-be early exits into a long candidate loop with 18 submissions and 17 distinct
PoCs. However, all available structured rewards remained at the same state:
`format=not_declared`, `root=not_reached`, `propagation=blocked_on_root`, and
`target=not_triggered`, with zero observed declared steps. Thus the mechanism strongly
increased exploration but supplied no improving runtime gradient on this sample.

`arvo_31705` reached the limit without a valid server submission. GPT repeatedly left
a long process active; its sole native tool selection consequently returned the
OpenHands "previous command still running" observation before reaching the server.
This is a tool-execution/liveness failure, so it cannot measure reward effectiveness.
