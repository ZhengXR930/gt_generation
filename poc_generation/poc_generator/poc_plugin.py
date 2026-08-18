#!/usr/bin/env python3
"""Compatibility entrypoint for the OpenHands PoC batch plugin."""

from openhands_backend.poc_plugin import *  # noqa: F401,F403
from openhands_backend.poc_plugin import main


if __name__ == "__main__":
    raise SystemExit(main())
