from reward_framework.adapters.base import (
    CLI_RUNNER,
    RewardCommand,
    RewardRequest,
    common_args,
    reward_environment,
    runner_python,
    sample_args,
)

NAME = "codex"
INSTALLER = "reward_framework.adapters.codex.install:install_workspace_skill_packet"


def build_command(request: RewardRequest) -> RewardCommand:
    args = [
        runner_python(),
        str(CLI_RUNNER),
        "--harness",
        NAME,
        *sample_args(request),
        *common_args(request),
        "--workspace-installer",
        INSTALLER,
        "--max-output-tokens",
        str(request.max_output_tokens),
    ]
    if request.reasoning_effort:
        args += ["--codex-reasoning-effort", request.reasoning_effort]
    args += list(request.extra_args)
    return RewardCommand(NAME, tuple(args), env=reward_environment(request))
