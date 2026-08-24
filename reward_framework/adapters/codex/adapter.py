from reward_framework.adapters.base import (
    CLI_RUNNER,
    RewardCommand,
    RewardRequest,
    arvo_args,
    common_args,
    reward_environment,
    runner_python,
)

NAME = "codex"
INSTALLER = "reward_framework.adapters.codex.install:install_workspace_skill_packet"


def build_command(request: RewardRequest) -> RewardCommand:
    if not request.is_arvo:
        raise ValueError("Codex non-ARVO execution is not implemented by harness_runtime")
    args = [
        runner_python(),
        str(CLI_RUNNER),
        "--harness",
        NAME,
        *arvo_args(request),
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
