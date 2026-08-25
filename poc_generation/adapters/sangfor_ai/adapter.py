from poc_generation.adapters.base import (
    Command,
    REPO_ROOT,
    Request,
    arvo_args,
    clean_environment,
    common_args,
    runner_python,
)

NAME = "sangfor_ai"
RUNNER = REPO_ROOT / "harness_runtime" / "sangfor_ai" / "wrapper.py"


def build_command(request: Request) -> Command:
    sample_args = arvo_args(request) if request.is_arvo else ["--sample-id", request.sample_id]
    args = [
        runner_python(),
        str(RUNNER),
        *sample_args,
        *common_args(request),
    ]
    if request.is_arvo:
        args += ["--harness-profile", "standard"]
    if request.openhands_repo:
        args += ["--openhands-repo", str(request.openhands_repo)]
    args += list(request.extra_args)
    return Command(NAME, tuple(args), env=clean_environment())
