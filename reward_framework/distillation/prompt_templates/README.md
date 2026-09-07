# Distillation Prompt Templates

Every file here becomes a prompt for one distillation role, and nothing else does:

- `diagnostician.md`: explains why one sample stopped where the evaluator says it stopped.
- `teacher.md`: turns batch diagnoses and the sample-level/aggregated pools into candidate skill updates.
- `curator.md`: rewrites or skips Teacher updates before the skill packet is changed.
- `correction.md`: reviews real post-update batch behavior and keeps, modifies, removes, or rolls back the tentative packet.

The coding agent is not one of these. It is the subject of the experiment rather than part
of the machinery, it is swappable across all four harnesses, and its prompt is
`reward_framework/prompt.txt`. The boundary between the two sides is written down in
`reward_framework/coding_agent_boundary.md`.

Python code appends machine-readable JSON payloads to these templates. Edit these documents to change role behavior without changing orchestration code.

## Two invariants the templates must keep stating

**The outcome is not an opinion.** `outcome`, `deepest_stage`, and `first_failed_stage` come from `reward_framework/distillation/outcome.py`, which reads the executed R1..R5 reachability ladder. The diagnostician is given that verdict and explains it; it does not produce it.

**`proposal` and `evidence` are separated on purpose.** Only `proposal` can reach `SKILL.md`, and `SKILL.md` is read by the agent at test time, so proposals must be transferable and free of source files, function names, line numbers, projects, sample ids, and concrete constants. `evidence` carries the specifics, stays in the run artifacts, and is never packaged. `lint.py` enforces this mechanically; the templates exist so the models do not waste updates on text that will be rejected.
