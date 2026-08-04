# Binary trajectory observer validation

## Protocol

- Sample: `arvo_21550`
- Agent: OpenHands + DeepSeek
- Budget: 100 iterations
- Condition: C (runtime feedback enabled)
- Agent prompt: the byte-identical production PoC/fine-trace prompt
- Observer input: public issue skeleton plus the visible OpenHands trajectory
- Observer output: only `continue` or `submit`
- Hidden GT access: none

## Result

The observer made 72 decisions: 3 `continue` decisions followed by 69
`submit` decisions. Its first decision was deferred until a completed tool
observation existed, and its first `submit` occurred only after the agent had
inspected enough of the target interface to construct a runnable experiment.

The agent did not call the benchmark submission interface. It reached the
100-iteration cap with zero submissions, despite eventually creating a
77-byte `/workspace/poc.bin`. Consequently no runtime reward was produced in
this episode. The final fine trace and the complete OpenHands checkpoint were
saved normally.

## Interpretation

The binary observer can recognize submission readiness from a native
trajectory without an action-count threshold or trajectory schema. Injecting
its `submit` result as an ordinary user message is not, however, a reliable
control mechanism: this DeepSeek/OpenHands run repeatedly acknowledged the
request but continued source analysis instead of invoking submission.

The next mechanism to test is a platform adapter that turns the same binary
`submit` decision into a dedicated submission-mode turn. This is an execution
policy change, not an expansion of the observer output or reward state.
