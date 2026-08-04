# Candidate-bootstrap monitor: four-sample exploratory run

Date: 2026-08-01

## Setup

- Agent: OpenHands with DeepSeek (`deepseek/deepseek-chat`).
- Budget: 100 iterations.
- Inputs available to the monitor: public issue description, the derived
  issue skeleton, and recent agent actions. Hidden GT and GT invariants were
  not used.
- Policy: an LLM decides only when the agent has enough public evidence to
  attempt a runnable candidate. Submission detection, iteration ceilings, and
  runtime outcomes are deterministic. After the first submission, the normal
  runtime feedback loop is used.
- The four cases were selected from the earlier 50-iteration exploratory run
  in which each had produced zero submissions.

## Results

| Sample | Bootstrap signal | First submission | Submissions | Distinct PoCs | Crash | Assessment |
|---|---:|---:|---:|---:|---:|---|
| `arvo_3325` | action 12 (ceiling) | none | 0 | 0 | no | Valid run; reminders did not change the action policy. |
| `arvo_25530` | action 4 (LLM-ready) | action 13 | 1 | 1 | yes | Fast conversion from analysis to a successful candidate. |
| `arvo_21550` | action 4 (LLM-ready) | action 25 | 3 | 3 | yes, third PoC | The first two runnable candidates exited 0; iterative revision eventually crashed. |
| `arvo_31705` | action 4 in retained retry (LLM-ready) | none | 0 | 0 | no | Protocol-invalid after three attempts; do not use as an ordinary negative outcome. |

All four retained runs have a checkpoint and final fine trace. Every actual
submission has its own PoC, candidate trace, request, result, and runtime
output directory. All four submitted PoCs were byte-distinct and all four
per-submission traces were structurally valid.

## What this run establishes

The mechanism is feasible, but this run is not yet a causal effectiveness
claim. On two assessable examples the monitor converted a historical
zero-submission behavior into candidate generation, and both ultimately
crashed. `arvo_21550` is the clearest evidence for the intended loop: candidate
creation was activated, two non-crashing experiments were retained, and a
third distinct candidate succeeded.

The result also exposes the main limitation. A natural-language intervention
is advisory. On `arvo_3325`, the agent ignored repeated bootstrap reminders;
on `arvo_31705`, model/protocol errors dominated. An LLM monitor can recognize
that a candidate is now warranted, but cannot by itself guarantee that the
next action constructs or submits one.

The next version should therefore separate two components:

1. **Semantic monitor:** issue-only LLM judgment of whether a minimally
   falsifiable candidate can be formed.
2. **Deterministic action gate:** once bootstrap is required, temporarily
   restrict broad exploration and require a concrete candidate-building or
   submission action, while leaving normal coding tools available.

A controlled evaluation still needs the same samples, model settings,
100-iteration budget, retry policy, and repeated seeds for baseline versus
monitor. Report three outcomes separately: probability of any submission,
runtime phase advancement across submissions, and final crash rate. Comparing
the earlier 50-iteration history directly with this 100-iteration run would
overstate the evidence.

## Artifacts

- Aggregate result: `results/candidate_monitor_batch_summary.json`
- Per-sample artifacts: `results/condition_c_monitor/<sample>/`
- Monitor transition log: `candidate_state_machine.jsonl` in each sample
  directory
