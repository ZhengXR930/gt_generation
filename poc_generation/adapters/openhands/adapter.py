from poc_generation.adapters.base import (
    Command,
    OPENHANDS_LOCAL_RUNNER,
    OPENHANDS_RUNNER,
    Request,
    arvo_args,
    clean_environment,
    common_args,
    runner_python,
)

NAME = "openhands"


def build_command(request: Request) -> Command:
    runner = OPENHANDS_RUNNER if request.is_arvo else OPENHANDS_LOCAL_RUNNER
    sample_args = (
        arvo_args(request) if request.is_arvo else ["--sample-id", request.sample_id]
    )
    args = [runner_python(), str(runner), *sample_args, *common_args(request)]
    if request.is_arvo:
        args += ["--harness-profile", "standard"]
    if request.openhands_repo:
        args += ["--openhands-repo", str(request.openhands_repo)]
    args += list(request.extra_args)
    return Command(NAME, tuple(args), env=clean_environment())
