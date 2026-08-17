#!/usr/bin/env python3
"""Compatibility entrypoint for OpenHands ARVO PoC generation."""

from openhands_backend.run_sample import *  # noqa: F401,F403
from openhands_backend.run_sample import main


if __name__ == "__main__":
    main()
