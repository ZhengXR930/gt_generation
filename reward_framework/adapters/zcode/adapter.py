"""Build a skill-enabled ZCode CLI command for reward-framework runs."""

from reward_framework.adapters.base import (
    CLI_RUNNER,
    RewardCommand,
    RewardRequest,
    common_args,
    reward_environment,
    runner_python,
    sample_args,
)


NAME = "zcode"
# The installer eagerly appends the stage packet to the task prompt and also
# exports the packet for provenance.  This is runtime-neutral and works for
# ZCode, whose native skill discovery is not Codex-specific.
INSTALLER = "reward_framework.adapters.zcode.install:install_workspace_skill_packet"


def build_command(request: RewardRequest) -> RewardCommand:
    args = [
        runner_python(),
        str(CLI_RUNNER),
        "--harness",
        NAME,
        *sample_args(request),
        *common_args(request),
        "--max-output-tokens",
        str(request.max_output_tokens),
        "--zcode-reasoning-effort",
        request.reasoning_effort or "max",
    ]
    if request.skill_packet is not None:
        args += ["--workspace-installer", INSTALLER]
    args += list(request.extra_args)
    return RewardCommand(NAME, tuple(args), env=reward_environment(request))
