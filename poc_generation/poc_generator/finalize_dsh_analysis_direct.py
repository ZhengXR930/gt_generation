#!/usr/bin/env python3
"""Compatibility entrypoint for direct DSH analysis finalization."""

from dsh.finalize_dsh_analysis_direct import *  # noqa: F401,F403
from dsh.finalize_dsh_analysis_direct import main


if __name__ == "__main__":
    raise SystemExit(main())
