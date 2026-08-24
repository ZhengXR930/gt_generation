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
    if not request.is_arvo:
        raise ValueError("Claude non-ARVO execution is not implemented by harness_runtime")
    args = [
        runner_python(),
        str(CLI_RUNNER),
        "--harness",
        NAME,
        *arvo_args(request),
        *common_args(request),
        *request.extra_args,
    ]
    return Command(NAME, tuple(args), env=clean_environment())
