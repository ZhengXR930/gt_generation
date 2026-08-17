#!/usr/bin/env python3
"""Compatibility entrypoint for DeepSeek Harness local PoC generation."""

from dsh.run_deepseek_harness_local_sample import *  # noqa: F401,F403
from dsh.run_deepseek_harness_local_sample import main


if __name__ == "__main__":
    raise SystemExit(main())
