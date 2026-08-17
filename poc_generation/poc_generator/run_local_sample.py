#!/usr/bin/env python3
"""Compatibility entrypoint for OpenHands local PoC generation."""

from openhands_backend.run_local_sample import *  # noqa: F401,F403
from openhands_backend.run_local_sample import main


if __name__ == "__main__":
    raise SystemExit(main())
