#!/usr/bin/env python3
"""Compatibility entrypoint for the DeepSeek Harness local batch runner."""

from dsh.run_dsh_local_batch import *  # noqa: F401,F403
from dsh.run_dsh_local_batch import main


if __name__ == "__main__":
    raise SystemExit(main())
