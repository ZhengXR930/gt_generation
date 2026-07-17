# Evaluation

Evaluation contains exactly two parts:

```text
reasoning/     verified assertion probes answered in the frozen subject session
reachability/ deterministic R1-R5 execution reachability scoring
```

There is no evaluator agent, trajectory judge, external interpreter, reasoning
recorder MCP, or enhancement loop.

## Reasoning

1. At PoC submission, agent finish, or the iteration limit, keep the same subject
   session but enter an irreversible answering phase.
2. The independent evaluation-prober reads frozen files from `gt_results/`; it never
   invokes a GT stage, recompiles a target, perturbs a PoC, or changes an assertion.
3. A constrained questioning agent converts eligible verified assertions into frozen
   questions. Its output schema is only `{id, question}`; deterministic code attaches
   the unchanged oracle answer and writes derived artifacts under `probe_results/`.
4. Freeze the rendered questions before starting the subject. After exploration,
   apply the `evaluation-prober` contract inside that same subject session; no new
   answering or judging session is created.
5. Remove all built-in/MCP/shell/file/browser/recall tools before answering.
6. Inject only the public questions into the existing conversation. Do not replay raw
   context or trajectory; retain them only for auditing. Keep oracle answers hidden and
   grade the canonical relation deterministically.

Probe construction is a derived evaluation step, not a GT stage. The selector attempts
one Reach, one Mechanism, and one Propagation probe from the immutable verified pool;
Propagation may contain multiple named slots. Public IDs are anonymous (`q001`, `q002`,
...), and the three dimensions are equally weighted. Changes to this selection policy do
not invalidate or regenerate GT.

The selector does not enumerate every assertion. Reach comes from the verified source
sink, Mechanism from a root-bound required assertion, and Propagation from a connected
cross-event transition path ending at a sink. It compares each gold expression (including
the complementary violated form of a comparison) with the frozen issue and default crash
trace. If any dimension has no non-leaked verified candidate, probe generation fails with
`unavailable` instead of inventing or weakening a question.

Assertion execution and perturbation validation belong to Stage 04 under
`gt_generation/gt_toolkit/assertions.py`. Evaluation consumes their frozen JSON outputs;
it does not import or invoke that validator.

Before running the subject, set `QUESTIONING_AGENT_COMMAND` to an agent-harness adapter
accepting `--role-file`, `--input`, and `--output`. The evaluation launcher uses
`reasoning/questioning_agent.md` and writes
`probe_results/<sample_id>/assertion_probes.json`. Changing selection, leakage checks,
wording, or scoring reruns only this light probe pipeline; `gt_results/` remains unchanged.

`reasoning/openhands/zero_tool_probe.patch` is the tracked integration for the ignored
OpenHands 0.33 checkout. The evaluation launcher applies it idempotently.

## Reachability

```bash
PYTHONPATH=evaluation_mode python3 -m reachability.cli \
  --gt ground_truth.json \
  --poc poc \
  --sanitizer-trace sanitizer_trace.txt \
  --out-dir reachability_out
```

The GDB/sanitizer execution engine and R1-R5 scoring both live in this package.
GT generation reuses them through `gt-toolkit reachability`; no separate shared
package is needed.
