"""Minimal pure-Python compatibility for OpenHands' optional ``pylcs`` import.

The pinned OpenHands runner uses only ``pylcs.lcs_sequence_length`` for edit
chunk localization. Some lightweight Alpine environments lack a C++ compiler,
so the upstream extension cannot be built. Keeping this exact one-function
compatibility module in the OpenHands subprocess PYTHONPATH lets the runner
start without changing OpenHands behavior outside that call boundary.
"""

from __future__ import annotations


def lcs_sequence_length(left: str, right: str) -> int:
    """Return the Longest Common Subsequence length for two strings."""
    if not left or not right:
        return 0
    if len(right) > len(left):
        left, right = right, left
    previous = [0] * (len(right) + 1)
    for left_char in left:
        current = [0]
        diagonal = 0
        for index, right_char in enumerate(right, start=1):
            above = previous[index]
            if left_char == right_char:
                value = diagonal + 1
            else:
                left_value = current[index - 1]
                value = above if above >= left_value else left_value
            current.append(value)
            diagonal = above
        previous = current
    return previous[-1]
