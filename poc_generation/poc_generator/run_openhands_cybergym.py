#!/usr/bin/env python3
"""Compatibility entrypoint for the OpenHands PoC backend."""

from openhands_backend.run_openhands_cybergym import *  # noqa: F401,F403
from openhands_backend.run_openhands_cybergym import logger, main


if __name__ == "__main__":
    import logging

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(levelname)s] %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    main()
