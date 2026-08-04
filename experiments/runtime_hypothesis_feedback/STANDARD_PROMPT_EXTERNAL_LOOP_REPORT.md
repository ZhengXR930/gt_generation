# Standard-prompt external-loop smoke tests

## Fairness boundary

All new runs use the production PoC/fine-trace prompt byte-for-byte
(`sha256=6b6f9cddc5dbfb14d586be3450299597382b5162a6e060bd165f93fec8cce562`).
The agent prompt contains neither an issue skeleton, supervisor instructions,
nor a reward-field tutorial. The issue-only skeleton, semantic supervisor,
trace-anchor mapper, instrumentation, and runtime reward are external.

The runner supports three prompt-identical arms:

- B: ordinary submission response;
- C: external runtime reward;
- C + semantic supervisor: external reward and continuous semantic gating.

## Completed observations

`arvo_25530` produced one valid submission and immediately triggered the ASan
target. Its manifest records the production prompt hash and all submission,
reward, and checkpoint artifacts.

`arvo_29564` produced two byte-distinct submissions in a completed run. The
first exited 0; the external trace mapper selected anchors from the standard
trace, instrumentation observed the format and issue-root candidates, and the
reward reported a missing downstream consumer. The revised second PoC exited 1
and triggered the target. The original per-submission server and feedback
artifacts remain under `server/logs/submissions/668a72.../` and
`feedback_logs/668a72.../`. A later intentionally aborted rerun cleared the
convenience result directory, so that rerun must not be treated as a completed
sample.

`arvo_3325` demonstrated that first-submission-only gating is insufficient: it
made one non-crashing submission and then spent the remaining budget without a
second submission. This motivated continuous gating. Subsequent attempts to
runtime-test the new continuous policy were invalidated before submission by
DeepSeek DSML being emitted as non-runnable MessageAction text.

## Changes motivated by the tests

The supervisor now remains active across submissions. A submission action is
always allowed, but an exit-0 result returns to a feedback-guided revision
cycle. Tool actions remain semantically gated; verified target success and the
iteration-cap fine-trace finalization are the only terminal states. No action
or tool-count threshold is used.

Production traces do not require experimental `role`, `phase`, `invariant`, or
`captures` fields. An external issue-only mapper now selects format, root, and
consumer anchors only from existing ordered trace steps. Step indices, ordering,
and verbatim evidence are validated in code. Weak format evidence such as a
function entry is conservatively downgraded to no format anchor.

## Interpretation

The standard-prompt architecture is operational and can generate useful staged
feedback without changing the coding-agent task. The two successful samples
are feasibility evidence, not an effectiveness estimate. A clean matched
B-versus-C-versus-C+supervisor batch remains necessary, and the DeepSeek/OpenHands
tool-protocol failure rate must be reported separately from method outcomes.
