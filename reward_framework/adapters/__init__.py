"""Agent-platform adapters."""

from .base import CallbackAdapter, PlatformAdapter
from .openhands import (
    OpenHandsAdapter,
    create_openhands_adapter,
    install_openhands_reward_framework,
)

__all__ = [
    "CallbackAdapter", "PlatformAdapter", "OpenHandsAdapter",
    "create_openhands_adapter", "install_openhands_reward_framework",
]
