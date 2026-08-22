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

Level 1:
- L1.A-submit-loop
- L1.B-evidence-gain-gate
- L1.C-analysis-history-state
- L1.D-helper-safety

Level 2:
- L2.A-reproduction-loop
- L2.B-five-part-working-representation
- L2.C-candidate-feedback-repair
- L2.D-learned-reproduction-lessons
- L2.E-helper-safety

Initial helper scripts:

```text
level1_submission_verification/helpers/submit_preflight.py
level1_submission_verification/helpers/submit_history.py
level1_submission_verification/helpers/candidate_diff.py
level2_vulnerability_reproduction/helpers/candidate_plan.py
level2_vulnerability_reproduction/helpers/issue_code_alignment.py
```

Deliberately absent from the initial packet:

- no `analysis_writer_check.py` hard schema gate;
- no fixed `initial_repro_spec.py` helper, because the five-part hypothesis is a skill stage;
- no `runtime_evidence_summary.py`, because test-time Level 2 should not require dynamic instrumentation.
