"""Path-matching helpers for comparing GT locations to recorded locations.

The evaluators score the agent's structured recorder node-trace, not fuzzy
trajectory text, so only path normalization/suffix matching lives here.
"""

from __future__ import annotations


def _normalize_path(path: str) -> str:
    path = (path or "").strip("'\"")
    for marker in ("/src-vul/", "/src/"):
        if marker in path:
            return path.split(marker, 1)[1]
    return path.lstrip("./")


def path_suffix_matches(gt_path: str, observed_path: str) -> bool:
    gt = _normalize_path(gt_path)
    obs = _normalize_path(observed_path)
    return bool(gt and obs) and (obs.endswith(gt) or gt.endswith(obs))
