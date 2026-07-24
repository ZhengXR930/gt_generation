#!/usr/bin/env python3
"""Minimal DeepSeek client for the reasoning scorer's Tier-2 judge.

Deliberately the same model family the evaluated subject runs on (DeepSeek):
the judge only decides whether a verified fact is *present* in a trace, and
using the subject's own family avoids importing an outside model's knowledge
into grading. Reads DEEPSEEK_API_KEY from the environment or the repo-root
config.txt. Self-contained so the evaluator has no dependency on the PoC-
generation code.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JUDGE_MODEL = "deepseek-chat"
_BASE_URL = "https://api.deepseek.com"
_client = None


def _api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    cfg = REPO_ROOT / "config.txt"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError(f"DEEPSEEK_API_KEY not found in env or {cfg}")


def client():
    """Lazily-constructed OpenAI-compatible client pointed at DeepSeek."""
    global _client
    if _client is None:
        from openai import OpenAI  # imported lazily so non-judging paths need no dep
        _client = OpenAI(base_url=_BASE_URL, api_key=_api_key())
    return _client
