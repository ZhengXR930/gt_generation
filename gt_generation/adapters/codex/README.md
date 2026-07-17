# Codex / generic-CLI adapter

There is nothing to generate for Codex or any other coding-agent CLI: the
portable roles are plain Markdown and are fed directly as prompts by
`runner.py`. The only per-CLI thing is *how the agent binary is invoked*, which
is a single environment variable.

## How it works

`workflow.json` uses this template for every agent stage:

```
agent_command_template:
  ${GT_AGENT_COMMAND} --role-file {role_file} --sample {sample_path} --result-dir {result_dir}
```

`runner.py` renders `{role_file}` (`gt_generation/roles/gt_generator.md`),
`{sample_path}`, `{result_dir}`, `{repo_root}`, `{code_root}`, and the `sample.*`
fields, then executes the command with `gt_generation/` on `PYTHONPATH`. Point
`GT_AGENT_COMMAND` at whatever CLI wraps your model.

## Examples

```bash
# Codex CLI
export GT_AGENT_COMMAND='./gt_generation/adapters/codex/gt_agent_codex.sh'
# Claude Code (headless)
export GT_AGENT_COMMAND='claude -p'
# A shell shim you control
export GT_AGENT_COMMAND='./gt_generation/adapters/codex/gt_agent_wrapper_example.sh'

python3 gt_generation/runner.py --sample gt_generation/sample.example.json --resume
```

Your `GT_AGENT_COMMAND` wrapper is responsible for:

1. reading the role prompt (`--role-file`) as the system/instruction text,
2. giving the agent access to the sample inputs and `--result-dir`,
3. letting the agent call `python3 -m gt_toolkit ...` for deterministic checks.

Working sources and sample images are deliberately not deleted by an agent adapter.
Cleanup belongs after the whole per-sample pipeline has passed, so a failed stage can
be retried without pulling and extracting the sample again.

Deterministic stages (like `02_validate`) have their own `command_template` and
do not use `GT_AGENT_COMMAND` at all — they call `gt_toolkit` directly, so the
schema gate is identical across every CLI.
