#!/usr/bin/env python3
"""Compatibility entrypoint for DSH checkpoint analysis recovery."""

from dsh.recover_dsh_analysis_from_checkpoint import *  # noqa: F401,F403
from dsh.recover_dsh_analysis_from_checkpoint import main


if __name__ == "__main__":
    raise SystemExit(main())
