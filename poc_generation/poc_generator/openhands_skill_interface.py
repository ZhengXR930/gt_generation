"""Compatibility re-export for the OpenHands adapter contract.

New code should import from `reward_framework.adapters.openhands.contract`.
This module remains only to avoid breaking older local scripts.
"""

from reward_framework.adapters.openhands.contract import *  # noqa: F401,F403
