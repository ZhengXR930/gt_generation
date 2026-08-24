from reward_framework.adapters.base import (
    CLI_RUNNER,
    RewardCommand,
    RewardRequest,
    arvo_args,
    common_args,
    reward_environment,
    runner_python,
)

NAME = "claude"
INSTALLER = "reward_framework.adapters.claude.install:install_workspace_skill_packet"


def build_command(request: RewardRequest) -> RewardCommand:
    if not request.is_arvo:
        raise ValueError("Claude non-ARVO execution is not implemented by harness_runtime")
    args = [
        runner_python(),
        str(CLI_RUNNER),
        "--harness",
        NAME,
        *arvo_args(request),
        *common_args(request),
        "--workspace-installer",
        INSTALLER,
    ]
    args += list(request.extra_args)
    return RewardCommand(NAME, tuple(args), env=reward_environment(request))
