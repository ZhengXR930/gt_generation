from poc_generation.adapters.base import (
    Command,
    DSH_LOCAL_RUNNER,
    DSH_RUNNER,
    Request,
    arvo_args,
    clean_environment,
    common_args,
    runner_python,
)

NAME = "deepseek_harness"


def build_command(request: Request) -> Command:
    runner = DSH_RUNNER if request.is_arvo else DSH_LOCAL_RUNNER
    sample_args = arvo_args(request) if request.is_arvo else ["--sample-id", request.sample_id]
    args = [
        runner_python(),
        str(runner),
        *sample_args,
        *common_args(request),
        "--no-run-reachability-after-generation",
        *request.extra_args,
    ]
    return Command(NAME, tuple(args), env=clean_environment())
