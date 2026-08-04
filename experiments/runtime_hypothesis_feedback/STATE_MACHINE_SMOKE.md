# Candidate-bootstrap state-machine smoke test

Date: 2026-08-01

The first 20-iteration `arvo_3325` smoke episode exercised the monitored
OpenHands entry point successfully. The issue-only monitor evaluated the event
history after 4, 8, and 12 tool actions. It returned `insufficient` at 4 and 8,
then `ready` at 12. The controller injected the candidate-bootstrap user event
and entered `bootstrap_required` without disabling tools.

After the intervention, the agent narrowed its broad repository search to a
concrete candidate input and wrote a PoC file on its twentieth tool action. It
did not have another iteration in which to invoke `submit.sh`, so this episode
does not establish a PoC-success improvement.

This run exposed a controller error: a later monitor decision described the
agent as progressing rather than stalled, causing the pending bootstrap to go
unreinforced. The controller now repeats the bootstrap instruction after the
configured pending interval regardless of that semantic stall label. Direct
transition tests cover `orient -> bootstrap_required`, the pending repeat, and
the deterministic `submit.sh -> feedback_loop` transition.

Two subsequent live attempts were unusable because DeepSeek emitted malformed
DSML/tool calls from the beginning of the episode. These are model-protocol
failures and provide no evidence about the state machine. A full evaluation
must retry and exclude such episodes using the same protocol-validity policy as
the earlier batch.
