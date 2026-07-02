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

`runner.py` renders `{role_file}` (e.g. `gt_generation/roles/02_gt_generator.md`),
`{sample_path}`, `{result_dir}`, `{repo_root}`, `{code_root}`, and the `sample.*`
fields, then executes the command with `gt_generation/` on `PYTHONPATH`. Point
`GT_AGENT_COMMAND` at whatever CLI wraps your model.

## Examples

```bash
# Codex CLI
export GT_AGENT_COMMAND='codex exec --full-auto --prompt-file'
# Claude Code (headless)
export GT_AGENT_COMMAND='claude -p'
# A shell shim you control
export GT_AGENT_COMMAND='./scripts/gt_agent_wrapper_example.sh'

python3 gt_generation/runner.py --sample gt_generation/sample.example.json --resume
```

Your `GT_AGENT_COMMAND` wrapper is responsible for:

1. reading the role prompt (`--role-file`) as the system/instruction text,
2. giving the agent access to the sample inputs and `--result-dir`,
3. letting the agent call `python3 -m gt_toolkit ...` for deterministic checks.

Deterministic stages (like `06_validate`) have their own `command_template` and
do not use `GT_AGENT_COMMAND` at all — they call `gt_toolkit` directly, so the
schema gate is identical across every CLI.
