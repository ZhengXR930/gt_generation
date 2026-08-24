"""The four baseline PoC-generation harness adapters."""

from importlib import import_module

from .base import Command, Request

HARNESSES = ("openhands", "codex", "claude", "deepseek_harness")

def get_adapter(name: str):
    normalized = name.strip().lower().replace("-", "_")
    if normalized not in HARNESSES:
        raise ValueError(f"unsupported harness {name!r}; expected {', '.join(HARNESSES)}")
    return import_module(f"poc_generation.adapters.{normalized}.adapter")

def build_command(request: Request) -> Command:
    return get_adapter(request.harness).build_command(request)

__all__ = ["Command", "Request", "HARNESSES", "build_command", "get_adapter"]
