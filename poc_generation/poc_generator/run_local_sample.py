#!/usr/bin/env python3
"""Compatibility entrypoint for OpenHands local PoC generation."""

try:
    from .openhands_backend.run_local_sample import *  # noqa: F401,F403
    from .openhands_backend.run_local_sample import main
except ImportError:  # Support direct execution from poc_generation/poc_generator.
    from openhands_backend.run_local_sample import *  # noqa: F401,F403
    from openhands_backend.run_local_sample import main


if __name__ == "__main__":
    raise SystemExit(main())
