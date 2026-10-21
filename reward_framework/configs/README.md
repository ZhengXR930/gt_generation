# DSH + DeepSeek configurations

Only the two canonical runtime configurations are kept here:

- `dsh_deepseek_4096_max.json`
- `dsh_deepseek_32768_max.json`

Both use DeepSeek's official DSH endpoint, `deepseek-v4-flash`, and
`reasoning_effort: max`. They default to the immutable initial-skill snapshot
and expose the current evolving packet through `skill_packets`.
The top-level reward runner defaults to the 4096-token file; use the 32768 file
explicitly for the high-budget arm.
The 4096 file selects the frozen TRAIN partition and the 32768 file selects the
disjoint TEST partition. An explicit `--sample`, `--samples-file`, or
`--sample-selector` overrides that default roster for a smoke run.
The same choice is available without switching files via
`--max-output-tokens 4096` or `--max-output-tokens 32768` (default: 4096).

Select the skill without copying or editing a configuration:

```bash
python -m reward_framework.run_harness \
  --config reward_framework/configs/dsh_deepseek_32768_max.json \
  --skill-variant initial --sample arvo_123

python -m reward_framework.run_harness \
  --config reward_framework/configs/dsh_deepseek_32768_max.json \
  --skill-variant evolving --sample arvo_123
```

An explicit `--skill-packet` still takes precedence over the named variant.
Pass samples on the command line (or through the existing selector/file
options); the canonical configs intentionally do not pin an old smoke or batch
roster.
