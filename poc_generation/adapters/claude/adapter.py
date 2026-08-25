from poc_generation.adapters.base import (
    CLI_RUNNER,
    Command,
    Request,
    arvo_args,
    clean_environment,
    common_args,
    runner_python,
)

NAME = "claude"


def build_command(request: Request) -> Command:
    sample_args = arvo_args(request) if request.is_arvo else ["--sample-id", request.sample_id]
    args = [
        runner_python(),
        str(CLI_RUNNER),
        "--harness",
        NAME,
        *sample_args,
        *common_args(request),
        *request.extra_args,
    ]
    return Command(NAME, tuple(args), env=clean_environment())
