"""Make evaluation packages importable in tests.

The evaluation code is organized under `evaluation_mode/` (reasoning, reachability).

Tests import these packages by their top-level names, so add each root to
sys.path the same way the run_*.sh scripts set PYTHONPATH.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _name in ("evaluation_mode",):
    _path = _ROOT / _name
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
