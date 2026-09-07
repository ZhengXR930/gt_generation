# The Coding Agent Boundary

The coding agent is the **subject** of the experiment, not a part of the distillation
machinery. It is whatever agent we are trying to improve: today Codex with
`gpt-5.5-2026-04-24`, tomorrow Claude, OpenHands, or DeepSeek Harness. Swapping it must
change nothing about how distillation works.

That is the opposite of the three distillation roles. The diagnostician, Teacher, and
Curator run as Codex subsessions (`harness_runtime/subsession.py`) and are deliberately
*not* swappable: they are measurement instruments, and holding them fixed is what keeps
diagnoses comparable across batches. The coding agent is the thing being measured.

```
                distillation                     benchmark
  ┌────────────────────────────────────┐   ┌────────────────────────┐
  │ diagnostician ─ teacher ─ curator  │   │  coding agent          │
  │ codex subsessions, fixed           │   │  codex / claude /      │
  │ prompts: distillation/             │   │  openhands / dsh       │
  │          prompt_templates/*.md     │   │  prompt: prompt.txt    │
  └────────────────────────────────────┘   └────────────────────────┘
                     │                                  ▲
                     └────── skill packet ──────────────┘
                        the only thing that crosses
```

The skill packet is the single channel between the two sides, and it goes one way:
distillation writes it, the coding agent reads it. Nothing else crosses. No diagnostic
score, no ground truth, no teacher feedback, no run history.

## Both arms are given the same task

`reward_framework/prompt.txt` is **byte-identical** to `poc_generation/prompt.txt`, and a
test enforces that. The packet is never mentioned in it. Every harness already has its own
way to surface a skill — native Agent Skills under `$CODEX_HOME/skills` and `.claude/`, a
bundle for DeepSeek Harness, a workspace copy plus a bootstrap read for OpenHands — so a
mention would add nothing except an instruction the baseline arm never receives. With the
prompts identical, the only difference between the arms is the packet itself.

The corollary is that delivery is a *silent* treatment: if an agent's skill discovery does
not fire, the packet had no effect and the run still looks normal. That is a thing to
verify in the trajectories of the first batch, not to paper over from the prompt.

## Path placeholders

Placeholders live only inside the packet, never in a task prompt. A skill document names
locations through `${SKILL_PACKET_DIR}`, `${HELPERS_DIR}`, `${STATE_DIR}`, and
`${WORKSPACE}`, and the installing adapter substitutes them at install time via
`render_skill_paths`, reporting the concrete locations as `agent_paths`.

Never write a concrete workspace path into a skill document — that is what would tie a
distilled packet to one harness.

## What must never cross into the coding agent

Do not add batch-specific ground truth, reachability results, evaluator diagnostics, or
teacher feedback to the coding-agent prompt.

That restriction covers the packet as well, not just the prompt. The diagnostician and
Teacher read ground truth during training, but everything they produce passes the lint in
`reward_framework/distillation/lint.py` before it can be written into `SKILL.md`, because
the packet is an input to the agent at test time. Evidence and rationale stay in the run
artifacts and are never packaged.
