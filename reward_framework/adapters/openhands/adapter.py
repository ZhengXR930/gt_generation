from reward_framework.adapters.base import (
    OPENHANDS_LOCAL_RUNNER,
    OPENHANDS_RUNNER,
    RewardCommand,
    RewardRequest,
    arvo_args,
    common_args,
    reward_environment,
    runner_python,
)

NAME = "openhands"
INSTALLER = "reward_framework.adapters.openhands.install:install_workspace_skill_packet"


def build_command(request: RewardRequest) -> RewardCommand:
    runner = OPENHANDS_RUNNER if request.is_arvo else OPENHANDS_LOCAL_RUNNER
    sample_args = arvo_args(request) if request.is_arvo else ["--sample-id", request.sample_id]
    args = [
        runner_python(),
        str(runner),
        *sample_args,
        *common_args(request),
        "--workspace-installer",
        INSTALLER,
    ]
    if request.is_arvo:
        args += ["--harness-profile", "standard"]
    if request.openhands_repo:
        args += ["--openhands-repo", str(request.openhands_repo)]
    args += list(request.extra_args)
    return RewardCommand(NAME, tuple(args), env=reward_environment(request))
