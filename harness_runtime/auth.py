"""Shared model/API-key helpers for harness runners."""

from __future__ import annotations

import os
from pathlib import Path


GT_ROOT = Path(__file__).resolve().parent.parent


def load_env_key(var_name: str) -> str:
    if os.environ.get(var_name):
        return os.environ[var_name]
    cfg = GT_ROOT / "config.txt"
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(f"{var_name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"{var_name} not found in env or {cfg}")


def default_api_key_env(model: str) -> str:
    normalized = model[len("openai/"):] if model.startswith("openai/") else model
    if normalized.startswith("deepseek"):
        return "DEEPSEEK_API_KEY"
    if normalized.startswith("claude-"):
        return "ANTHROPIC_API_KEY"
    if normalized.startswith(("gpt-", "o3", "o4")):
        return "OPENAI_API_KEY"
    return "LLM_API_KEY"
