# Offline Static Skill Distillation

Builds train observation Markdown, TraeCLI prompt files, and a static two-layer skill packet scaffold.

Use direct script execution on this server:

```bash
python3 reward_framework/offline_static_distillation/cli.py --help
```

Typical static setup flow:

```bash
python3 reward_framework/offline_static_distillation/cli.py scaffold-skill-packet \
  --out reward_framework/offline_static_distillation_runs/skill_packet_initial

python3 reward_framework/offline_static_distillation/cli.py validate-skill-packet \
  --skill-packet reward_framework/offline_static_distillation_runs/skill_packet_initial
```

The scaffolded packet uses coarse skill chunks:

Submission Skill:
- S.A-submit-loop
- S.B-evidence-gain-gate
- S.C-analysis-history-state
- S.D-helper-safety

Reproduction Skill:
- R.A-reproduction-loop
- R.B-five-part-working-representation
- R.C-candidate-feedback-repair
- R.D-learned-reproduction-lessons
- R.E-helper-safety

Initial helper scripts:

```text
submission_skill/helpers/submit_preflight.py
submission_skill/helpers/submit_history.py
submission_skill/helpers/candidate_diff.py
reproduction_skill/helpers/candidate_plan.py
reproduction_skill/helpers/issue_code_alignment.py
```

Deliberately absent from the initial packet:

- no `analysis_writer_check.py` hard schema gate;
- no fixed `initial_repro_spec.py` helper, because the five-part hypothesis is a skill stage;
- no `runtime_evidence_summary.py`, because the test-time Reproduction Skill should not require dynamic instrumentation.
