from reward_framework.adapters.base import (
    DSH_LOCAL_RUNNER,
    DSH_RUNNER,
    REPO_ROOT,
    RewardCommand,
    RewardRequest,
    arvo_args,
    common_args,
    reward_environment,
    runner_python,
)

NAME = "deepseek_harness"
INSTALLER = "reward_framework.adapters.deepseek_harness.install:install_workspace_skill_packet"
DSH_ARVO_BASELINE_PROMPT = REPO_ROOT / "reward_framework" / "prompts" / "deepseek_harness_arvo_baseline.txt"


def build_command(request: RewardRequest) -> RewardCommand:
    runner = DSH_RUNNER if request.is_arvo else DSH_LOCAL_RUNNER
    sample_args = arvo_args(request) if request.is_arvo else ["--sample-id", request.sample_id]
    prompt_file = DSH_ARVO_BASELINE_PROMPT
    reasoning_effort = request.reasoning_effort
    if reasoning_effort == "medium":
        reasoning_effort = "high"
    args = [
        runner_python(),
        str(runner),
        *sample_args,
        *common_args(request, prompt_file),
        "--workspace-installer",
        INSTALLER,
        "--reasoning-effort",
        reasoning_effort,
        "--no-run-reachability-after-generation",
    ]
    args += list(request.extra_args)
    return RewardCommand(NAME, tuple(args), env=reward_environment(request, prompt_file))
