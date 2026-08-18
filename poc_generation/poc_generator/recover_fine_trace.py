#!/usr/bin/env python3
"""Compatibility entrypoint for OpenHands fine-trace recovery."""

from openhands_backend.recover_fine_trace import *  # noqa: F401,F403
from openhands_backend.recover_fine_trace import main


if __name__ == "__main__":
    raise SystemExit(main())
