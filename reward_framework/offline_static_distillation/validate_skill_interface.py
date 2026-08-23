#!/usr/bin/env python3
"""Compatibility CLI for validating the OpenHands skill adapter interface."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reward_framework.adapters.openhands.validate import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
