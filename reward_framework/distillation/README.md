# Distillation implementation

This package contains one harness/model-configurable implementation of the
four-role learning loop:

- `diagnosis_view.py`, `outcome.py`: normalize evaluator and trajectory evidence;
- `roles.py`, `prompts.py`: execute the role sessions using the four reviewed
  templates under `prompt_templates/`;
- `teacher_evidence.py`, `pools.py`: build paired and rolling evidence;
- `skill_packet.py`: validate, materialize, and update the two runtime skills;
- `cli.py`: the sole batch orchestration entry point;
- `audit.py`, `lint.py`, `reference.py`, `artifacts.py`, `splits.py`: validation,
  artifact access, and frozen split support.

The role templates are intentionally separate from the coding-agent task prompt
(`reward_framework/prompt.txt`). There is no second distillation script or
alternate coding-agent prompt in this directory. Historical/final
role-template copies are not loaded by `prompts.py` and are not active inputs.
The role runner can execute through DSH, Codex, or ZCode; the active learning
run is selected by its run configuration.
