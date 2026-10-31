# Two-layer staged evolving skill packet

This is the stable materialization of the final DSH + DeepSeek TRAIN
distillation packet used for the TEST evolving arm. The agent receives two
runtime skills, with the causal and submission stages kept together so the
initial context remains coherent. Teacher and Curator route later lessons to
the stage identified by the diagnosis.

The superseded two-lesson packet is preserved under
`skill_packets/_archive/dsh_deepseek_evolving_two_layer_staged_100_100_pre_final/`.
