# Unified Reward Framework

This package implements an external, GT-blind runtime-reward loop for
vulnerability-reproduction agents.  It is independent of `gt_results/`, the
frozen evaluator, and the historical reward experiments.

## Controller-owned flow

```text
issue + vulnerable source
        |
        v
Codex Spec Agent -> Admission -> Source -> Root -> Propagation -> Target
        |
coding trajectory <-> persistent observer (continue/request_submission)
        |
submit_candidate -> checkpoint + content deduplication
        |
Codex probe plan -> instrumentation adapter -> trusted runtime facts
        |
deterministic ordered stage gate -> Codex factual feedback
        |
trigger success or continue until the iteration limit
```

The issue is a trusted task statement.  The candidate fine trace is an
untrusted hypothesis.  Runtime evidence and the independent trigger oracle are
authoritative.  Codex never owns stage status, score, deduplication, execution,
or termination.

## State

Each task state directory contains:

- `task_context.json`: immutable public issue, source manifest, and frozen Spec;
- `observation_state.json`: the complete coding-agent trajectory and controller
  transitions;
- `candidates/`: one folder per unique PoC, a latest trace, and every submission
  attempt/checkpoint reference;
- `evidence/`: one immutable runtime/assessment/feedback record per attempt;
- `agent_view/`: a source-only Reward-Agent view plus public state documents.

Duplicate PoCs reuse the unique candidate folder, retain the latest trace, and
still receive a separate attempt record.  `candidate_stats()` reports total,
unique, duplicate, and unique ratio.

## Codex roles

`CodexBackend` defaults to `gpt-5.5` and invokes `codex exec` with an ephemeral
session, read-only sandbox, ignored user configuration/rules, and a strict JSON
schema.  One backend instance provides four persistent logical roles:

1. one-time Spec initialization;
2. full-trajectory submission observation;
3. per-candidate passive probe planning;
4. evidence-grounded factual feedback.

The backend audits commands and rejects sessions that access parent/absolute
paths, GT/result paths, patches, sanitizer traces, network tools, environment
variables, or git history.

## Runtime adapter contract

`InstrumentationBackend.verify()` receives the immutable PoC, exact submitted
trace, source-validated probe plan, and attempt output directory.  It returns:

```python
RawRuntimeReport(
    exit_code=...,
    stdout=...,
    stderr=...,
    trigger_observed=...,
    stage_observations={"admission": StageStatus.CONFIRMED, ...},
    facts=(RuntimeFact(...), ...),
    instrumentation_available=True,
)
```

`CommandInstrumentationBackend` can wrap any platform runtime.  Its optional
instrumentation command receives `{poc}`, `{trace}`, `{probe_plan}`,
`{stage_report}`, and `{output_dir}` placeholders.  This is the bridge for the
existing ARVO GDB runner and future SEC/OSS-specific local RuntimeSpecs; the
core framework contains no dataset-specific breakpoint assumptions.
The exact external JSON contract is frozen in `schemas/stage_report.json`;
instrumentation may report only direct `confirmed`, `refuted`, `unresolved`, or
`not_reached` observations.  `not_declared` and `observed_but_blocked` remain
controller-owned derived states.

`ArvoGDBInstrumentationBackend` is the production ARVO implementation. It
repairs a stale trace line by locating the selected statement in the current
public source, compiles source/function/line breakpoints, evaluates an optional
side-effect-free `condition`, runs the real `/bin/arvo` harness independently,
and converts only GDB hits/values into stage facts. Inputs are staged briefly
under the mounted repository and compact evidence is copied into the attempt;
the prepared binary is never archived.

## Integration

OpenHands and Codex adapters use callback-based message injection and checkpoint
creation. The OpenHands production hook registers `SUBMIT_CANDIDATE_TOOL`,
lowers its sandboxed call through a token-authenticated local transport to
`RewardFramework.submit_candidate()`, records every visible trajectory event,
and invokes `observe_trajectory()` at completed action or turn boundaries. A
requested submission blocks stale non-submission actions, while ordinary
investigation remains agent-controlled. No fixed tool-count trigger is used.

Only trigger success and `reach_iteration_limit()` terminate an episode.  At
the limit, the framework injects the required no-tool final fine-trace request.

For the CyberGym/OpenHands launcher, enable the formal wiring with:

```bash
OPENHANDS_REWARD_FRAMEWORK=1 <existing run command>
```

The launcher preserves the standard evaluation prompt and replaces only its
submission transport. It sets the task/workspace/state variables and selects
`reward_framework.openhands_entrypoint`; the Reward Agent model defaults to
`gpt-5.5` and can be changed with `REWARD_FRAMEWORK_MODEL`.
