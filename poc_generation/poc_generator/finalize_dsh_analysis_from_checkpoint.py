#!/usr/bin/env python3
"""Compatibility entrypoint for DSH checkpoint analysis finalization."""

from dsh.finalize_dsh_analysis_from_checkpoint import *  # noqa: F401,F403
from dsh.finalize_dsh_analysis_from_checkpoint import main


if __name__ == "__main__":
    raise SystemExit(main())
