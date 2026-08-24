"""The four reward-framework harness adapters."""

from importlib import import_module

from .base import RewardCommand, RewardRequest

HARNESSES = ("openhands", "codex", "claude", "deepseek_harness")


def get_adapter(name: str):
    normalized = name.strip().lower().replace("-", "_")
    if normalized not in HARNESSES:
        raise ValueError(
            f"unsupported reward harness {name!r}; expected {', '.join(HARNESSES)}"
        )
    return import_module(f"reward_framework.adapters.{normalized}.adapter")


def build_command(request: RewardRequest) -> RewardCommand:
    return get_adapter(request.harness).build_command(request)


__all__ = [
    "HARNESSES",
    "RewardCommand",
    "RewardRequest",
    "build_command",
    "get_adapter",
]
