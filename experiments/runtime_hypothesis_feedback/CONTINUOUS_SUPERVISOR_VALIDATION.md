# C + continuous supervisor validation

## Scope

This validation ran condition C only. It did not run a new B/C comparison. The
agent received the unchanged production PoC-generation prompt from
`poc_generation/poc_generator/template/prompt.txt` (SHA-256
`6b6f9cddc5dbfb14d586be3450299597382b5162a6e060bd165f93fec8cce562`). The
prompt contains neither the issue skeleton nor supervisor/reward instructions.
The issue-derived observer, semantic gate, runtime reward, and feedback
injection remained external to the agent prompt and had no hidden-GT access.

The supervisor was configured to remain active before and after every
submission. A previous submission does not disable action classification, and
an exit-0 submission is not a terminal result. Target-triggered success and
iteration-cap fine-trace finalization remain terminal exceptions.

## Durable results

Four runs produced complete manifests and checkpoints. `arvo_3325` was
interrupted during a protocol retry and is excluded from the assessable set.

| sample | terminal status | submissions | crashing PoCs | gate decisions | decisions after first submission | redirects | result |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `arvo_29564` | success | 1 | 1 | 19 | 0 | 0 | first submission triggered target |
| `arvo_21550` | agent finished | 1 | 0 | 43 | 32 | 0 | first submission exited 0; no revision submitted |
| `arvo_31332` | success | 1 | 1 | 28 | 0 | 0 | first submission triggered target |
| `arvo_31705` | iteration cap | 0 | 0 | 79 | n/a | 0 | no candidate submitted |

All four retained runs have a checkpoint and valid final fine trace. Every
submission has an independent directory containing the PoC, candidate trace,
runtime result/output, request, and reward feedback.

## What the run establishes

The continuity property works. `arvo_21550` is direct evidence: after its
non-crashing first submission, the supervisor classified 32 further proposed
actions. It therefore did not shut off on first submission. It also prevented
an exit-0 result from being treated as verified success.

The current supervisor is not yet an effective controller. All 169 retained
gate decisions were classified as `advances_candidate`; none caused a
redirect. In `arvo_21550`, 32 post-feedback decisions produced neither a
redirect nor a second candidate. In `arvo_31705`, 79 decisions produced no
candidate at all. The two crashes occurred on the first submission, so they
cannot be attributed to continuous post-submission supervision.

The present classifier is therefore functioning as a continuously invoked
observer, but not as a mechanism that reliably turns analysis and reward into
candidate revisions. The failure is not that monitoring stops too early. The
failure is that the per-action label `advances_candidate` is too permissive:
focused source inspection and debugging can receive that label indefinitely
without a material change to the runnable candidate.

## Platform limitation observed separately

DeepSeek/OpenHands also produced malformed tool calls, especially after the
first `arvo_21550` submission. Those protocol errors reduce the chance of a
revision, but they do not explain the whole result: `arvo_31705` accumulated 79
allowed gate decisions without a submission. Supervisor policy and platform
reliability should therefore be reported as separate failure sources.

## Verdict and next design requirement

The implementation passes **continuous activation**, but this validation does
not support an **efficacy** claim. More C-only samples with this classifier
would mostly repeat the same failure mode.

The next supervisor should judge state progress rather than only the apparent
purpose of each proposed action. It should retain the unresolved reward stage
and current candidate commitment, name the concrete candidate field an action
is expected to change, and verify after the observation that the field or
candidate actually changed. Once no concrete field blocks a runnable next
candidate, further analysis should be redirected to submission. This remains
semantic and platform-independent and does not require tool-call or iteration
thresholds.
