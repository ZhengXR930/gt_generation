"""Make harness packages importable in tests after the eval/enhance/shared split.

The harness code is organized into three roots:
  - shared/          (recorder_core, reachability_core, patch_evaluator_core, ...)
  - evaluation_mode/ (external_interpreter, evaluator, ...)
  - enhance_mode/    (candidate_synthesis_core, enhance_mcp_servers, ...)

Tests import these packages by their top-level names, so add each root to
sys.path the same way the run_*.sh scripts set PYTHONPATH.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _name in ("shared", "evaluation_mode", "enhance_mode"):
    _path = _ROOT / _name
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
